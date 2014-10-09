# -*- coding: utf-8 -*-
# __init__.py
# Copyright (C) 2013 LEAP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
"""
Key Manager is a Nicknym agent for LEAP client.
"""
# let's do a little sanity check to see if we're using the wrong gnupg
import sys

try:
    from gnupg.gnupg import GPGUtilities
    assert(GPGUtilities)  # pyflakes happy
    from gnupg import __version__ as _gnupg_version
    from pkg_resources import parse_version
    assert(parse_version(_gnupg_version) >= parse_version('1.2.3'))

except (ImportError, AssertionError):
    print "*******"
    print "Ooops! It looks like there is a conflict in the installed version "
    print "of gnupg."
    print
    print "Disclaimer: Ideally, we would need to work a patch and propose the "
    print "merge to upstream. But until then do: "
    print
    print "% pip uninstall python-gnupg"
    print "% pip install gnupg"
    print "*******"
    sys.exit(1)

import logging
import requests

from leap.common.check import leap_assert, leap_assert_type
from leap.common.events import signal
from leap.common.events import events_pb2 as proto
from leap.common.decorators import memoized_method

from leap.keymanager.errors import KeyNotFound, KeyAttributesDiffer

from leap.keymanager.keys import (
    EncryptionKey,
    build_key_from_dict,
    KEYMANAGER_KEY_TAG,
    TAGS_PRIVATE_INDEX,
)
from leap.keymanager.openpgp import (
    OpenPGPKey,
    OpenPGPScheme,
)

logger = logging.getLogger(__name__)


#
# The Key Manager
#

class KeyManager(object):

    #
    # server's key storage constants
    #

    OPENPGP_KEY = 'openpgp'
    PUBKEY_KEY = "user[public_key]"

    def __init__(self, address, nickserver_uri, soledad, token=None,
                 ca_cert_path=None, api_uri=None, api_version=None, uid=None,
                 gpgbinary=None):
        """
        Initialize a Key Manager for user's C{address} with provider's
        nickserver reachable in C{nickserver_uri}.

        :param address: The email address of the user of this Key Manager.
        :type address: str
        :param nickserver_uri: The URI of the nickserver.
        :type nickserver_uri: str
        :param soledad: A Soledad instance for local storage of keys.
        :type soledad: leap.soledad.Soledad
        :param token: The token for interacting with the webapp API.
        :type token: str
        :param ca_cert_path: The path to the CA certificate.
        :type ca_cert_path: str
        :param api_uri: The URI of the webapp API.
        :type api_uri: str
        :param api_version: The version of the webapp API.
        :type api_version: str
        :param uid: The user's UID.
        :type uid: str
        :param gpgbinary: Name for GnuPG binary executable.
        :type gpgbinary: C{str}
        """
        self._address = address
        self._nickserver_uri = nickserver_uri
        self._soledad = soledad
        self._token = token
        self.ca_cert_path = ca_cert_path
        self.api_uri = api_uri
        self.api_version = api_version
        self.uid = uid
        # a dict to map key types to their handlers
        self._wrapper_map = {
            OpenPGPKey: OpenPGPScheme(soledad, gpgbinary=gpgbinary),
            # other types of key will be added to this mapper.
        }
        # the following are used to perform https requests
        self._fetcher = requests
        self._session = self._fetcher.session()

    #
    # utilities
    #

    def _key_class_from_type(self, ktype):
        """
        Return key class from string representation of key type.
        """
        return filter(
            lambda klass: str(klass) == ktype,
            self._wrapper_map).pop()

    def _get(self, uri, data=None):
        """
        Send a GET request to C{uri} containing C{data}.

        :param uri: The URI of the request.
        :type uri: str
        :param data: The body of the request.
        :type data: dict, str or file

        :return: The response to the request.
        :rtype: requests.Response
        """
        leap_assert(
            self._ca_cert_path is not None,
            'We need the CA certificate path!')
        res = self._fetcher.get(uri, data=data, verify=self._ca_cert_path)
        # Nickserver now returns 404 for key not found and 500 for
        # other cases (like key too small), so we are skipping this
        # check for the time being
        # res.raise_for_status()

        # Responses are now text/plain, although it's json anyway, but
        # this will fail when it shouldn't
        # leap_assert(
        #     res.headers['content-type'].startswith('application/json'),
        #     'Content-type is not JSON.')
        return res

    def _put(self, uri, data=None):
        """
        Send a PUT request to C{uri} containing C{data}.

        The request will be sent using the configured CA certificate path to
        verify the server certificate and the configured session id for
        authentication.

        :param uri: The URI of the request.
        :type uri: str
        :param data: The body of the request.
        :type data: dict, str or file

        :return: The response to the request.
        :rtype: requests.Response
        """
        leap_assert(
            self._ca_cert_path is not None,
            'We need the CA certificate path!')
        leap_assert(
            self._token is not None,
            'We need a token to interact with webapp!')
        res = self._fetcher.put(
            uri, data=data, verify=self._ca_cert_path,
            headers={'Authorization': 'Token token=%s' % self._token})
        # assert that the response is valid
        res.raise_for_status()
        return res

    @memoized_method(invalidation=300)
    def _fetch_keys_from_server(self, address):
        """
        Fetch keys bound to C{address} from nickserver and insert them in
        local database.

        :param address: The address bound to the keys.
        :type address: str

        :raise KeyNotFound: If the key was not found on nickserver.
        """
        # request keys from the nickserver
        res = None
        try:
            res = self._get(self._nickserver_uri, {'address': address})
            res.raise_for_status()
            server_keys = res.json()
            # insert keys in local database
            if self.OPENPGP_KEY in server_keys:
                self._wrapper_map[OpenPGPKey].put_ascii_key(
                    server_keys['openpgp'])
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise KeyNotFound(address)
            logger.warning("HTTP error retrieving key: %r" % (e,))
            logger.warning("%s" % (res.content,))
        except Exception as e:
            logger.warning("Error retrieving key: %r" % (e,))

    #
    # key management
    #

    def send_key(self, ktype):
        """
        Send user's key of type C{ktype} to provider.

        Public key bound to user's is sent to provider, which will sign it and
        replace any prior keys for the same address in its database.

        :param ktype: The type of the key.
        :type ktype: KeyType

        :raise KeyNotFound: If the key was not found in local database.
        """
        leap_assert(
            ktype is OpenPGPKey,
            'For now we only know how to send OpenPGP public keys.')
        # prepare the public key bound to address
        pubkey = self.get_key(
            self._address, ktype, private=False, fetch_remote=False)
        data = {
            self.PUBKEY_KEY: pubkey.key_data
        }
        uri = "%s/%s/users/%s.json" % (
            self._api_uri,
            self._api_version,
            self._uid)
        self._put(uri, data)
        signal(proto.KEYMANAGER_DONE_UPLOADING_KEYS, self._address)

    def get_key(self, address, ktype, private=False, fetch_remote=True):
        """
        Return a key of type C{ktype} bound to C{address}.

        First, search for the key in local storage. If it is not available,
        then try to fetch from nickserver.

        :param address: The address bound to the key.
        :type address: str
        :param ktype: The type of the key.
        :type ktype: KeyType
        :param private: Look for a private key instead of a public one?
        :type private: bool
        :param fetch_remote: If key not found in local storage try to fetch
                             from nickserver
        :type fetch_remote: bool

        :return: A key of type C{ktype} bound to C{address}.
        :rtype: EncryptionKey
        :raise KeyNotFound: If the key was not found both locally and in
                            keyserver.
        """
        logger.debug("getting key for %s" % (address,))
        leap_assert(
            ktype in self._wrapper_map,
            'Unkown key type: %s.' % str(ktype))
        try:
            signal(proto.KEYMANAGER_LOOKING_FOR_KEY, address)
            # return key if it exists in local database
            key = self._wrapper_map[ktype].get_key(address, private=private)
            signal(proto.KEYMANAGER_KEY_FOUND, address)

            return key
        except KeyNotFound:
            signal(proto.KEYMANAGER_KEY_NOT_FOUND, address)

            # we will only try to fetch a key from nickserver if fetch_remote
            # is True and the key is not private.
            if fetch_remote is False or private is True:
                raise

            signal(proto.KEYMANAGER_LOOKING_FOR_KEY, address)
            self._fetch_keys_from_server(address)  # might raise KeyNotFound
            key = self._wrapper_map[ktype].get_key(address, private=False)
            signal(proto.KEYMANAGER_KEY_FOUND, address)

            return key

    def get_all_keys(self, private=False):
        """
        Return all keys stored in local database.

        :param private: Include private keys
        :type private: bool

        :return: A list with all keys in local db.
        :rtype: list
        """
        return map(
            lambda doc: build_key_from_dict(
                self._key_class_from_type(doc.content['type']),
                doc.content['address'],
                doc.content),
            self._soledad.get_from_index(
                TAGS_PRIVATE_INDEX,
                KEYMANAGER_KEY_TAG,
                '1' if private else '0'))

    def gen_key(self, ktype):
        """
        Generate a key of type C{ktype} bound to the user's address.

        :param ktype: The type of the key.
        :type ktype: KeyType

        :return: The generated key.
        :rtype: EncryptionKey
        """
        signal(proto.KEYMANAGER_STARTED_KEY_GENERATION, self._address)
        key = self._wrapper_map[ktype].gen_key(self._address)
        signal(proto.KEYMANAGER_FINISHED_KEY_GENERATION, self._address)

        return key

    #
    # Setters/getters
    #

    def _get_token(self):
        return self._token

    def _set_token(self, token):
        self._token = token

    token = property(
        _get_token, _set_token, doc='The session token.')

    def _get_ca_cert_path(self):
        return self._ca_cert_path

    def _set_ca_cert_path(self, ca_cert_path):
        self._ca_cert_path = ca_cert_path

    ca_cert_path = property(
        _get_ca_cert_path, _set_ca_cert_path,
        doc='The path to the CA certificate.')

    def _get_api_uri(self):
        return self._api_uri

    def _set_api_uri(self, api_uri):
        self._api_uri = api_uri

    api_uri = property(
        _get_api_uri, _set_api_uri, doc='The webapp API URI.')

    def _get_api_version(self):
        return self._api_version

    def _set_api_version(self, api_version):
        self._api_version = api_version

    api_version = property(
        _get_api_version, _set_api_version, doc='The webapp API version.')

    def _get_uid(self):
        return self._uid

    def _set_uid(self, uid):
        self._uid = uid

    uid = property(
        _get_uid, _set_uid, doc='The uid of the user.')

    #
    # encrypt/decrypt and sign/verify API
    #

    def encrypt(self, data, pubkey, passphrase=None, sign=None,
                cipher_algo='AES256'):
        """
        Encrypt C{data} using public @{key} and sign with C{sign} key.

        :param data: The data to be encrypted.
        :type data: str
        :param pubkey: The key used to encrypt.
        :type pubkey: EncryptionKey
        :param passphrase: The passphrase for the secret key used for the
                           signature.
        :type passphrase: str
        :param sign: The key used for signing.
        :type sign: EncryptionKey
        :param cipher_algo: The cipher algorithm to use.
        :type cipher_algo: str

        :return: The encrypted data.
        :rtype: str
        """
        leap_assert_type(pubkey, EncryptionKey)
        leap_assert(pubkey.__class__ in self._wrapper_map, 'Unknown key type.')
        leap_assert(pubkey.private is False, 'Key is not public.')
        return self._wrapper_map[pubkey.__class__].encrypt(
            data, pubkey, passphrase, sign, cipher_algo=cipher_algo)

    def decrypt(self, data, privkey, passphrase=None, verify=None):
        """
        Decrypt C{data} using private @{privkey} and verify with C{verify} key.

        :param data: The data to be decrypted.
        :type data: str
        :param privkey: The key used to decrypt.
        :type privkey: OpenPGPKey
        :param passphrase: The passphrase for the secret key used for
                           decryption.
        :type passphrase: str
        :param verify: The key used to verify a signature.
        :type verify: OpenPGPKey

        :return: The decrypted data.
        :rtype: str

        :raise InvalidSignature: Raised if unable to verify the signature with
                                 C{verify} key.
        """
        leap_assert_type(privkey, EncryptionKey)
        leap_assert(
            privkey.__class__ in self._wrapper_map,
            'Unknown key type.')
        leap_assert(privkey.private is True, 'Key is not private.')
        return self._wrapper_map[privkey.__class__].decrypt(
            data, privkey, passphrase, verify)

    def sign(self, data, privkey, digest_algo='SHA512', clearsign=False,
             detach=True, binary=False):
        """
        Sign C{data} with C{privkey}.

        :param data: The data to be signed.
        :type data: str

        :param privkey: The private key to be used to sign.
        :type privkey: EncryptionKey
        :param digest_algo: The hash digest to use.
        :type digest_algo: str
        :param clearsign: If True, create a cleartext signature.
        :type clearsign: bool
        :param detach: If True, create a detached signature.
        :type detach: bool
        :param binary: If True, do not ascii armour the output.
        :type binary: bool

        :return: The signed data.
        :rtype: str
        """
        leap_assert_type(privkey, EncryptionKey)
        leap_assert(
            privkey.__class__ in self._wrapper_map,
            'Unknown key type.')
        leap_assert(privkey.private is True, 'Key is not private.')
        return self._wrapper_map[privkey.__class__].sign(
            data, privkey, digest_algo=digest_algo, clearsign=clearsign,
            detach=detach, binary=binary)

    def verify(self, data, pubkey, detached_sig=None):
        """
        Verify signed C{data} with C{pubkey}, eventually using
        C{detached_sig}.

        :param data: The data to be verified.
        :type data: str
        :param pubkey: The public key to be used on verification.
        :type pubkey: EncryptionKey
        :param detached_sig: A detached signature. If given, C{data} is
                             verified using this detached signature.
        :type detached_sig: str

        :return: The signed data.
        :rtype: str
        """
        leap_assert_type(pubkey, EncryptionKey)
        leap_assert(pubkey.__class__ in self._wrapper_map, 'Unknown key type.')
        leap_assert(pubkey.private is False, 'Key is not public.')
        return self._wrapper_map[pubkey.__class__].verify(
            data, pubkey, detached_sig=detached_sig)

    def delete_key(self, key):
        """
        Remove C{key} from storage.

        May raise:
            openpgp.errors.KeyNotFound
            openpgp.errors.KeyAttributesDiffer

        :param key: The key to be removed.
        :type key: EncryptionKey
        """
        try:
            self._wrapper_map[type(key)].delete_key(key)
        except IndexError as e:
            leap_assert(False, "Unsupported key type. Error {0!r}".format(e))

    def put_key(self, key):
        """
        Put C{key} in local storage.

        :param key: The key to be stored. It can be ascii key or an OpenPGPKey
        :type key: str or OpenPGPKey
        """
        try:
            if isinstance(key, basestring):
                self._wrapper_map[OpenPGPKey].put_ascii_key(key)
            else:
                self._wrapper_map[type(key)].put_key(key)
        except IndexError as e:
            leap_assert(False, "Unsupported key type. Error {0!r}".format(e))

    def fetch_key(self, address, uri, ktype):
        """
        Fetch a public key for C{address} from the network and put it in
        local storage.

        Raises C{openpgp.errors.KeyNotFound} if not valid key on C{uri}.
        Raises C{openpgp.errors.KeyAttributesDiffer} if address don't match
        any uid on the key.

        :param address: The email address of the key.
        :type address: str
        :param uri: The URI of the key.
        :type uri: str
        :param ktype: The type of the key.
        :type ktype: KeyType
        """
        res = self._get(uri)
        if not res.ok:
            raise KeyNotFound(uri)

        # XXX parse binary keys
        pubkey, _ = self._wrapper_map[ktype].parse_ascii_key(res.content)
        if pubkey is None:
            raise KeyNotFound(uri)
        if pubkey.address != address:
            raise KeyAttributesDiffer("UID %s found, but expected %s"
                                      % (pubkey.address, address))
        self.put_key(pubkey)

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions
