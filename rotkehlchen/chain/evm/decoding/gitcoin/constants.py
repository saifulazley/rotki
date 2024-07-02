from typing import Final

from rotkehlchen.chain.evm.types import string_to_evm_address

GITCOIN_GRANTS_BULKCHECKOUT: Final = string_to_evm_address('0x7d655c57f71464B6f83811C55D84009Cd9f5221C')  # noqa: E501

DONATION_SENT: Final = b';\xb7B\x8b%\xf9\xbd\xad\x9b\xd2\xfa\xa4\xc6\xa7\xa9\xe5\xd5\x88&W\xe9l\x1d$\xccA\xc1\xd6\xc1\x91\n\x98'  # noqa: E501
PAYOUT_CLAIMED: Final = b'\xechF\x1f]L\xc4\\\x89\xe9\x14\xcb\x88&\xa9f\xc7=\xd3^_\x97\x81^\xce\n\x01\xff\xa4\xa0%\xa6'  # noqa: E501