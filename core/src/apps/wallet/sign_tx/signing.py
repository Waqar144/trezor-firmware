import gc
from micropython import const

from trezor import utils
from trezor.crypto import base58, bip32, der
from trezor.crypto.curve import secp256k1
from trezor.crypto.hashlib import sha256
from trezor.messages import FailureType, InputScriptType, OutputScriptType
from trezor.messages.SignTx import SignTx
from trezor.messages.TransactionType import TransactionType
from trezor.messages.TxInputType import TxInputType
from trezor.messages.TxOutputBinType import TxOutputBinType
from trezor.messages.TxOutputType import TxOutputType
from trezor.messages.TxRequest import TxRequest
from trezor.messages.TxRequestDetailsType import TxRequestDetailsType
from trezor.messages.TxRequestSerializedType import TxRequestSerializedType

from apps.common import address_type, coininfo, seed
from apps.wallet.sign_tx import (
    addresses,
    helpers,
    multisig,
    progress,
    scripts,
    segwit_bip143,
    tx_weight,
    writers,
)

if False:
    from typing import Dict, List, Optional, Tuple, Union

# the number of bip32 levels used in a wallet (chain and address)
_BIP32_WALLET_DEPTH = const(2)

# the chain id used for change
_BIP32_CHANGE_CHAIN = const(1)

# the maximum allowed change address.  this should be large enough for normal
# use and still allow to quickly brute-force the correct bip32 path
_BIP32_MAX_LAST_ELEMENT = const(1000000)


class SigningError(ValueError):
    pass


# Transaction signing
# ===
# see https://github.com/trezor/trezor-mcu/blob/master/firmware/signing.c#L84
# for pseudo code overview
# ===


class Bitcoin:
    async def signer(
        self, tx: SignTx, keychain: seed.Keychain, coin: coininfo.CoinInfo
    ) -> None:
        self.initialize(tx, keychain, coin)

        progress.init(self.tx.inputs_count, self.tx.outputs_count)

        # Phase 1
        # - check inputs, previous transactions, and outputs
        # - ask for confirmations
        # - check fee
        await self.phase1()

        # Phase 2
        # - sign inputs
        # - check that nothing changed
        await self.phase2()

    def initialize(
        self, tx: SignTx, keychain: seed.Keychain, coin: coininfo.CoinInfo
    ) -> None:
        self.coin = coin
        self.tx = helpers.sanitize_sign_tx(tx, self.coin)
        self.keychain = keychain

        self.multisig_fp = (
            multisig.MultisigFingerprint()
        )  # control checksum of multisig inputs
        self.wallet_path = (
            []
        )  # type: Optional[List[int]] # common prefix of input paths
        self.bip143_in = 0  # sum of segwit input amounts
        self.segwit = (
            {}
        )  # type: Dict[int, bool] # dict of booleans stating if input is segwit
        self.total_in = 0  # sum of input amounts
        self.total_out = 0  # sum of output amounts
        self.change_out = 0  # change output amount

        self.tx_req = TxRequest()
        self.tx_req.details = TxRequestDetailsType()

        # h_first is used to make sure the inputs and outputs streamed in Phase 1
        # are the same as in Phase 2 when signing legacy inputs.  it is thus not required to fully hash the
        # tx, as the SignTx info is streamed only once
        self.h_first = utils.HashWriter(sha256())  # not a real tx hash

        self.init_hash143()

    def init_hash143(self) -> None:
        self.hash143 = segwit_bip143.Bip143()  # BIP-0143 transaction hashing

    async def phase1(self) -> None:
        weight = tx_weight.TxWeightCalculator(
            self.tx.inputs_count, self.tx.outputs_count
        )

        # compute sum of input amounts (total_in)
        # add inputs to hash143 and h_first
        for i in range(self.tx.inputs_count):
            # STAGE_REQUEST_1_INPUT
            progress.advance()
            txi = await helpers.request_tx_input(self.tx_req, i, self.coin)
            weight.add_input(txi)
            await self.phase1_process_input(i, txi)

        txo_bin = TxOutputBinType()
        for i in range(self.tx.outputs_count):
            # STAGE_REQUEST_3_OUTPUT
            txo = await helpers.request_tx_output(self.tx_req, i, self.coin)
            txo_bin.amount = txo.amount
            txo_bin.script_pubkey = self.output_derive_script(txo)
            weight.add_output(txo_bin.script_pubkey)
            await self.phase1_confirm_output(i, txo, txo_bin)

        fee = self.total_in - self.total_out

        if fee < 0:
            self.on_negative_fee()

        # fee > (coin.maxfee per byte * tx size)
        if fee > (self.coin.maxfee_kb / 1000) * (weight.get_total() / 4):
            if not await helpers.confirm_feeoverthreshold(fee, self.coin):
                raise SigningError(FailureType.ActionCancelled, "Signing cancelled")

        if self.tx.lock_time > 0:
            if not await helpers.confirm_nondefault_locktime(self.tx.lock_time):
                raise SigningError(FailureType.ActionCancelled, "Locktime cancelled")

        if not await helpers.confirm_total(
            self.total_in - self.change_out, fee, self.coin
        ):
            raise SigningError(FailureType.ActionCancelled, "Total cancelled")

    async def phase1_process_input(self, i: int, txi: TxInputType) -> None:
        self.input_extract_wallet_path(txi)
        writers.write_tx_input_check(self.h_first, txi)
        self.hash143.add_prevouts(txi)  # all inputs are included (non-segwit as well)
        self.hash143.add_sequence(txi)

        if not addresses.validate_full_path(txi.address_n, self.coin, txi.script_type):
            await helpers.confirm_foreign_address(txi.address_n)

        if txi.multisig:
            self.multisig_fp.add(txi.multisig)
        else:
            self.multisig_fp.mismatch = True

        if txi.script_type in (
            InputScriptType.SPENDWITNESS,
            InputScriptType.SPENDP2SHWITNESS,
        ):
            await self.phase1_process_segwit_input(i, txi)
        elif txi.script_type in (
            InputScriptType.SPENDADDRESS,
            InputScriptType.SPENDMULTISIG,
        ):
            await self.phase1_process_nonsegwit_input(i, txi)
        else:
            raise SigningError(FailureType.DataError, "Wrong input script type")

    async def phase1_process_segwit_input(self, i: int, txi: TxInputType) -> None:
        if not txi.amount:
            raise SigningError(FailureType.DataError, "Segwit input without amount")
        self.segwit[i] = True
        self.bip143_in += txi.amount
        self.total_in += txi.amount

    async def phase1_process_nonsegwit_input(self, i: int, txi: TxInputType) -> None:
        self.segwit[i] = False
        self.total_in += await self.get_prevtx_output_value(
            txi.prev_hash, txi.prev_index
        )

    async def phase1_confirm_output(
        self, i: int, txo: TxOutputType, txo_bin: TxOutputBinType
    ) -> None:
        if self.change_out == 0 and self.output_is_change(txo):
            # output is change and does not need confirmation
            self.change_out = txo.amount
        elif not await helpers.confirm_output(txo, self.coin):
            raise SigningError(FailureType.ActionCancelled, "Output cancelled")

        writers.write_tx_output(self.h_first, txo_bin)
        self.hash143.add_output(txo_bin)
        self.total_out += txo_bin.amount

    def on_negative_fee(self) -> None:
        raise SigningError(FailureType.NotEnoughFunds, "Not enough funds")

    async def phase2(self) -> None:
        self.tx_req.serialized = None

        # Serialize inputs and sign non-segwit inputs.
        for i in range(self.tx.inputs_count):
            progress.advance()
            if self.segwit[i]:
                await self.phase2_serialize_segwit_input(i)
            else:
                await self.phase2_sign_nonsegwit_input(i)

        # Serialize outputs.
        tx_ser = TxRequestSerializedType()
        for i in range(self.tx.outputs_count):
            # STAGE_REQUEST_5_OUTPUT
            progress.advance()
            tx_ser.serialized_tx = await self.phase2_serialize_output(i)
            self.tx_req.serialized = tx_ser

        # Sign segwit inputs.
        any_segwit = True in self.segwit.values()
        for i in range(self.tx.inputs_count):
            progress.advance()
            if self.segwit[i]:
                # STAGE_REQUEST_SEGWIT_WITNESS
                witness, signature = await self.phase2_sign_segwit_input(i)
                tx_ser.serialized_tx = witness
                tx_ser.signature_index = i
                tx_ser.signature = signature
            elif any_segwit:
                # TODO what if a non-segwit input follows after a segwit input?
                tx_ser.serialized_tx += bytearray(
                    1
                )  # empty witness for non-segwit inputs
                tx_ser.signature_index = None
                tx_ser.signature = None

            self.tx_req.serialized = tx_ser

        self.write_sign_tx_footer(tx_ser.serialized_tx)

        await helpers.request_tx_finish(self.tx_req)

    async def phase2_serialize_segwit_input(self, i_sign: int) -> None:
        # STAGE_REQUEST_SEGWIT_INPUT
        txi_sign = await helpers.request_tx_input(self.tx_req, i_sign, self.coin)

        if not input_is_segwit(txi_sign):
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )
        self.input_check_wallet_path(txi_sign)
        # NOTE: No need to check the multisig fingerprint, because we won't be signing
        # the script here. Signatures are produced in STAGE_REQUEST_SEGWIT_WITNESS.

        key_sign = self.keychain.derive(txi_sign.address_n, self.coin.curve_name)
        key_sign_pub = key_sign.public_key()
        txi_sign.script_sig = self.input_derive_script(txi_sign, key_sign_pub)

        w_txi = writers.empty_bytearray(
            7 + len(txi_sign.prev_hash) + 4 + len(txi_sign.script_sig) + 4
        )
        if i_sign == 0:  # serializing first input => prepend headers
            self.write_sign_tx_header(w_txi, True)
        writers.write_tx_input(w_txi, txi_sign)
        self.tx_req.serialized = TxRequestSerializedType(serialized_tx=w_txi)

    async def phase2_sign_segwit_input(self, i: int) -> Tuple[bytearray, bytes]:
        txi = await helpers.request_tx_input(self.tx_req, i, self.coin)

        self.input_check_wallet_path(txi)
        self.input_check_multisig_fingerprint(txi)

        if not input_is_segwit(txi) or txi.amount > self.bip143_in:
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )
        self.bip143_in -= txi.amount

        key_sign = self.keychain.derive(txi.address_n, self.coin.curve_name)
        key_sign_pub = key_sign.public_key()
        hash143_hash = self.hash143.preimage_hash(
            self.coin,
            self.tx,
            txi,
            addresses.ecdsa_hash_pubkey(key_sign_pub, self.coin),
            self.get_hash_type(),
        )

        signature = ecdsa_sign(key_sign, hash143_hash)
        if txi.multisig:
            # find out place of our signature based on the pubkey
            signature_index = multisig.multisig_pubkey_index(txi.multisig, key_sign_pub)
            witness = scripts.witness_p2wsh(
                txi.multisig, signature, signature_index, self.get_hash_type()
            )
        else:
            witness = scripts.witness_p2wpkh(
                signature, key_sign_pub, self.get_hash_type()
            )

        return witness, signature

    async def phase2_sign_nonsegwit_input(self, i_sign: int) -> None:
        # hash of what we are signing with this input
        h_sign = utils.HashWriter(sha256())
        # same as h_first, checked before signing the digest
        h_second = utils.HashWriter(sha256())

        self.write_sign_tx_header(h_sign, has_segwit=False)

        for i in range(self.tx.inputs_count):
            # STAGE_REQUEST_4_INPUT
            txi = await helpers.request_tx_input(self.tx_req, i, self.coin)
            self.input_check_wallet_path(txi)
            writers.write_tx_input_check(h_second, txi)
            if i == i_sign:
                txi_sign = txi
                self.input_check_multisig_fingerprint(txi_sign)
                key_sign = self.keychain.derive(txi.address_n, self.coin.curve_name)
                key_sign_pub = key_sign.public_key()
                # for the signing process the script_sig is equal
                # to the previous tx's scriptPubKey (P2PKH) or a redeem script (P2SH)
                if txi_sign.script_type == InputScriptType.SPENDMULTISIG:
                    txi_sign.script_sig = scripts.output_script_multisig(
                        multisig.multisig_get_pubkeys(txi_sign.multisig),
                        txi_sign.multisig.m,
                    )
                elif txi_sign.script_type == InputScriptType.SPENDADDRESS:
                    txi_sign.script_sig = scripts.output_script_p2pkh(
                        addresses.ecdsa_hash_pubkey(key_sign_pub, self.coin)
                    )
                else:
                    raise SigningError(
                        FailureType.ProcessError, "Unknown transaction type"
                    )
            else:
                txi.script_sig = bytes()
            writers.write_tx_input(h_sign, txi)

        writers.write_varint(h_sign, self.tx.outputs_count)

        txo_bin = TxOutputBinType()
        for i in range(self.tx.outputs_count):
            # STAGE_REQUEST_4_OUTPUT
            txo = await helpers.request_tx_output(self.tx_req, i, self.coin)
            txo_bin.amount = txo.amount
            txo_bin.script_pubkey = self.output_derive_script(txo)
            writers.write_tx_output(h_second, txo_bin)
            writers.write_tx_output(h_sign, txo_bin)

        writers.write_uint32(h_sign, self.tx.lock_time)
        writers.write_uint32(h_sign, self.get_hash_type())

        # check the control digests
        if writers.get_tx_hash(self.h_first, False) != writers.get_tx_hash(h_second):
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )

        # if multisig, check if signing with a key that is included in multisig
        if txi_sign.multisig:
            multisig.multisig_pubkey_index(txi_sign.multisig, key_sign_pub)

        # compute the signature from the tx digest
        signature = ecdsa_sign(
            key_sign, writers.get_tx_hash(h_sign, double=self.coin.sign_hash_double)
        )

        # serialize input wittx_reqh correct signature
        gc.collect()
        txi_sign.script_sig = self.input_derive_script(
            txi_sign, key_sign_pub, signature
        )
        w_txi_sign = writers.empty_bytearray(
            5 + len(txi_sign.prev_hash) + 4 + len(txi_sign.script_sig) + 4
        )
        if i_sign == 0:  # serializing first input => prepend headers
            self.write_sign_tx_header(w_txi_sign, True in self.segwit.values())
        writers.write_tx_input(w_txi_sign, txi_sign)
        self.tx_req.serialized = TxRequestSerializedType(i_sign, signature, w_txi_sign)

    async def phase2_serialize_output(self, i: int) -> bytearray:
        txo = await helpers.request_tx_output(self.tx_req, i, self.coin)
        txo_bin = TxOutputBinType()
        txo_bin.amount = txo.amount
        txo_bin.script_pubkey = self.output_derive_script(txo)

        # serialize output
        w_txo_bin = writers.empty_bytearray(5 + 8 + 5 + len(txo_bin.script_pubkey) + 4)
        if i == 0:  # serializing first output => prepend outputs count
            writers.write_varint(w_txo_bin, self.tx.outputs_count)
        writers.write_tx_output(w_txo_bin, txo_bin)

        return w_txo_bin

    async def get_prevtx_output_value(self, prev_hash: bytes, prev_index: int) -> int:
        amount_out = 0  # sum of output amounts

        # STAGE_REQUEST_2_PREV_META
        tx = await helpers.request_tx_meta(self.tx_req, self.coin, prev_hash)

        if tx.outputs_cnt <= prev_index:
            raise SigningError(
                FailureType.ProcessError, "Not enough outputs in previous transaction."
            )

        txh = utils.HashWriter(sha256())

        # TODO set has_segwit correctly
        self.write_tx_header(txh, tx, has_segwit=False)
        writers.write_varint(txh, tx.inputs_cnt)

        for i in range(tx.inputs_cnt):
            # STAGE_REQUEST_2_PREV_INPUT
            txi = await helpers.request_tx_input(self.tx_req, i, self.coin, prev_hash)
            writers.write_tx_input(txh, txi)

        writers.write_varint(txh, tx.outputs_cnt)

        for o in range(tx.outputs_cnt):
            # STAGE_REQUEST_2_PREV_OUTPUT
            txo_bin = await helpers.request_tx_output(
                self.tx_req, o, self.coin, prev_hash
            )
            writers.write_tx_output(txh, txo_bin)
            if o == prev_index:
                amount_out = txo_bin.amount

        await self.write_prev_tx_footer(txh, tx, prev_hash)

        if (
            writers.get_tx_hash(txh, double=self.coin.sign_hash_double, reverse=True)
            != prev_hash
        ):
            raise SigningError(
                FailureType.ProcessError, "Encountered invalid prev_hash"
            )

        return amount_out

    # TX Helpers
    # ===

    def get_hash_type(self) -> int:
        SIGHASH_ALL = const(0x01)
        return SIGHASH_ALL

    def write_sign_tx_header(self, w: writers.Writer, has_segwit: bool) -> None:
        self.write_tx_header(w, self.tx, has_segwit)
        writers.write_varint(w, self.tx.inputs_count)

    def write_sign_tx_footer(self, w: writers.Writer) -> None:
        writers.write_uint32(w, self.tx.lock_time)

    def write_tx_header(
        self, w: writers.Writer, tx: Union[SignTx, TransactionType], has_segwit: bool
    ) -> None:
        writers.write_uint32(w, tx.version)  # nVersion
        if has_segwit:
            writers.write_varint(w, 0x00)  # segwit witness marker
            writers.write_varint(w, 0x01)  # segwit witness flag

    async def write_prev_tx_footer(
        self, w: writers.Writer, tx: TransactionType, prev_hash: bytes
    ) -> None:
        writers.write_uint32(w, tx.lock_time)

    # TX Outputs
    # ===

    def output_derive_script(self, o: TxOutputType) -> bytes:
        if o.script_type == OutputScriptType.PAYTOOPRETURN:
            return scripts.output_script_paytoopreturn(o.op_return_data)

        if o.address_n:
            # change output
            o.address = self.get_address_for_change(o)

        if self.coin.bech32_prefix and o.address.startswith(self.coin.bech32_prefix):
            # p2wpkh or p2wsh
            witprog = addresses.decode_bech32_address(
                self.coin.bech32_prefix, o.address
            )
            return scripts.output_script_native_p2wpkh_or_p2wsh(witprog)

        raw_address = self.get_raw_address(o)

        if address_type.check(self.coin.address_type, raw_address):
            # p2pkh
            pubkeyhash = address_type.strip(self.coin.address_type, raw_address)
            script = scripts.output_script_p2pkh(pubkeyhash)
            return script

        elif address_type.check(self.coin.address_type_p2sh, raw_address):
            # p2sh
            scripthash = address_type.strip(self.coin.address_type_p2sh, raw_address)
            script = scripts.output_script_p2sh(scripthash)
            return script

        raise SigningError(FailureType.DataError, "Invalid address type")

    def get_raw_address(self, o: TxOutputType) -> bytes:
        try:
            return base58.decode_check(o.address, self.coin.b58_hash)
        except ValueError:
            raise SigningError(FailureType.DataError, "Invalid address")

    def get_address_for_change(self, o: TxOutputType) -> str:
        try:
            input_script_type = helpers.CHANGE_OUTPUT_TO_INPUT_SCRIPT_TYPES[
                o.script_type
            ]
        except KeyError:
            raise SigningError(FailureType.DataError, "Invalid script type")
        node = self.keychain.derive(o.address_n, self.coin.curve_name)
        return addresses.get_address(input_script_type, self.coin, node, o.multisig)

    def output_is_change(self, o: TxOutputType) -> bool:
        if o.script_type not in helpers.CHANGE_OUTPUT_SCRIPT_TYPES:
            return False
        if o.multisig and not self.multisig_fp.matches(o.multisig):
            return False
        return (
            self.wallet_path is not None
            and self.wallet_path == o.address_n[:-_BIP32_WALLET_DEPTH]
            and o.address_n[-2] <= _BIP32_CHANGE_CHAIN
            and o.address_n[-1] <= _BIP32_MAX_LAST_ELEMENT
        )

    # Tx Inputs
    # ===

    def input_derive_script(
        self, i: TxInputType, pubkey: bytes, signature: bytes = None
    ) -> bytes:
        if i.script_type == InputScriptType.SPENDADDRESS:
            # p2pkh or p2sh
            return scripts.input_script_p2pkh_or_p2sh(
                pubkey, signature, self.get_hash_type()
            )

        if i.script_type == InputScriptType.SPENDP2SHWITNESS:
            # p2wpkh or p2wsh using p2sh

            if i.multisig:
                # p2wsh in p2sh
                pubkeys = multisig.multisig_get_pubkeys(i.multisig)
                witness_script_hasher = utils.HashWriter(sha256())
                scripts.write_output_script_multisig(
                    witness_script_hasher, pubkeys, i.multisig.m
                )
                witness_script_hash = witness_script_hasher.get_digest()
                return scripts.input_script_p2wsh_in_p2sh(witness_script_hash)

            # p2wpkh in p2sh
            return scripts.input_script_p2wpkh_in_p2sh(
                addresses.ecdsa_hash_pubkey(pubkey, self.coin)
            )
        elif i.script_type == InputScriptType.SPENDWITNESS:
            # native p2wpkh or p2wsh
            return scripts.input_script_native_p2wpkh_or_p2wsh()
        elif i.script_type == InputScriptType.SPENDMULTISIG:
            # p2sh multisig
            signature_index = multisig.multisig_pubkey_index(i.multisig, pubkey)
            return scripts.input_script_multisig(
                i.multisig, signature, signature_index, self.get_hash_type(), self.coin
            )
        else:
            raise SigningError(FailureType.ProcessError, "Invalid script type")

    def input_extract_wallet_path(self, txi: TxInputType) -> None:
        if self.wallet_path is None:
            return  # there was a mismatch in previous inputs
        address_n = txi.address_n[:-_BIP32_WALLET_DEPTH]
        if not address_n:
            self.wallet_path = None  # input path is too short
        elif not self.wallet_path:
            self.wallet_path = address_n  # this is the first input
        elif self.wallet_path != address_n:
            self.wallet_path = None  # paths don't match

    def input_check_wallet_path(self, txi: TxInputType) -> None:
        if self.wallet_path is None:
            return  # there was a mismatch in Phase 1, ignore it now
        address_n = txi.address_n[:-_BIP32_WALLET_DEPTH]
        if self.wallet_path != address_n:
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )

    def input_check_multisig_fingerprint(self, txi: TxInputType) -> None:
        if self.multisig_fp.mismatch is False:
            # All inputs in Phase 1 had matching multisig fingerprints, allowing a multisig change-output.
            if not txi.multisig or not self.multisig_fp.matches(txi.multisig):
                # This input no longer has a matching multisig fingerprint.
                raise SigningError(
                    FailureType.ProcessError, "Transaction has changed during signing"
                )


def input_is_segwit(i: TxInputType) -> bool:
    return (
        i.script_type == InputScriptType.SPENDWITNESS
        or i.script_type == InputScriptType.SPENDP2SHWITNESS
    )


def ecdsa_sign(node: bip32.HDNode, digest: bytes) -> bytes:
    sig = secp256k1.sign(node.private_key(), digest)
    sigder = der.encode_seq((sig[1:33], sig[33:65]))
    return sigder
