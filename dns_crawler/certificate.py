# Copyright © 2019-2023 CZ.NIC, z. s. p. o.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of dns-crawler.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from datetime import datetime

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509 import BasicConstraints
from cryptography.exceptions import UnsupportedAlgorithm

from .utils import drop_null_values


def patched_cryptography_init(self, ca, path_length):
    if not isinstance(ca, bool):
        raise TypeError("ca must be a boolean value")

    # if path_length is not None and not ca:
        # raise ValueError("path_length must be None when ca is False")

    if path_length is not None and (
        not isinstance(path_length, int) or path_length < 0
    ):
        raise TypeError(
            "path_length must be a non-negative integer or None"
        )

    self._ca = ca
    self._path_length = path_length


BasicConstraints.__init__ = patched_cryptography_init


def cert_datetime_to_iso(cert_date):
    return cert_date.strftime("%Y-%m-%d %H:%M:%S")


def parse_cert_name(cert, field):
    try:
        name = getattr(cert, field)
        return {k: v for k, v in [s.rfc4514_string().split("=", 1) for s in name.rdns]}
    except ValueError as e:
        return {"error": str(e)}


def format_cert_serial_number(serial):
    return f"{serial:016x}"


def get_pubkey_fingerprint(cert, hash):
    digest = hashes.Hash(hash, backend=default_backend())
    digest.update(cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo))
    return digest.finalize()


def parse_cert(cert):
    result = {}
    now = datetime.now()
    result["not_before"] = cert_datetime_to_iso(cert.not_valid_before)
    result["not_after"] = cert_datetime_to_iso(cert.not_valid_after)
    result["expired"] = cert.not_valid_after < now
    if result["expired"]:
        result["expired_for"] = (now - cert.not_valid_after).days
    result["expires_in"] = (cert.not_valid_after - now).days
    result["active"] = cert.not_valid_before < now and cert.not_valid_after > now
    result["validity_period"] = (cert.not_valid_after - cert.not_valid_before).days
    result["subject"] = parse_cert_name(cert, "subject")
    result["issuer"] = parse_cert_name(cert, "issuer")
    result["version"] = int(str(cert.version)[-1])
    result["serial"] = format_cert_serial_number(cert.serial_number)
    result["fingerprint"] = {
        "cert": {
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "sha512": cert.fingerprint(hashes.SHA512()).hex()
        },
        "pubkey": {
            "sha256": get_pubkey_fingerprint(cert, hashes.SHA256()).hex(),
            "sha512": get_pubkey_fingerprint(cert, hashes.SHA512()).hex()
        }
    }
    try:
        result["algorithm"] = cert.signature_hash_algorithm.name
    except UnsupportedAlgorithm:
        result["algorithm"] = None
    try:
        result["alt_names"] = [str(name.value) for name in cert.extensions.get_extension_for_oid(
            x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value]
    except (x509.extensions.ExtensionNotFound, ValueError):
        pass
    return drop_null_values(result)
