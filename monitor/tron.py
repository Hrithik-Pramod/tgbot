"""
Deposit monitor.

Polls each monitored wallet for incoming transfers, opens or extends the
matching trade, and notifies the Bridge.

WHICH ASSET
-----------
Production monitors TRC20 USDT. MONITOR_ASSET=TRX switches the poller to native
TRX for testing only (client request, 9 September 2026: a USDT transfer burns
roughly 13-27 TRX in energy, so repeated end-to-end tests get expensive, while a
TRX transfer costs a fraction of one TRX).

TRX is not a TRC20 token and does not appear on the TRC20 endpoints at all, so
this is a genuinely different request on both providers — not a filter change.
What it does and does not prove is worth being precise about:

  proves      polling, the cursor and its rewind, duplicate absorption, trade
              creation, the arithmetic, and every notification. That is the
              whole pipeline.
  leaves out  the TRC20 response parser itself, which is a different function.
              That one is pinned separately against captured live responses in
              tests/test_tronscan_contract.py.

So a TRX run is a real end-to-end test of everything except twenty lines of
parsing that have their own tests. Switch back to USDT before go-live; the
setting is the only thing that changes.

Design notes, because this is the part that goes wrong in production:

  * At-least-once, never at-most-once. A restart, an overlapping poll window or
    a retried request will re-deliver transactions already seen. Correctness
    comes from the UNIQUE (tx_hash, wallet_id) constraint in the database, not
    from the poller being careful. Losing a deposit is unrecoverable; seeing one
    twice is free.

  * The cursor is stored per wallet and only ever moves forward. After downtime
    the monitor resumes from the last transaction it recorded rather than
    rescanning from zero or, worse, skipping the gap.

  * The cursor is deliberately rewound by OVERLAP_MS on each poll. Block
    timestamps are not perfectly ordered, and a strict "greater than last seen"
    filter drops transactions that land on the boundary.

  * TronScan is primary (answer B7 - free, and the client has used it before);
    TronGrid is the fallback so a rate-limit or outage does not blind us.

  * Amounts are converted from the contract's integer units to Decimal via
    string, never through float.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from decimal import Decimal
from typing import Any, Optional

import httpx

from core.summary import render_payout_notice, tx_link

log = logging.getLogger(__name__)

# USDT on TRON has 6 decimals. So does TRX (1 TRX = 1,000,000 SUN), so the same
# scale serves both — but they are the same number by coincidence, not by rule,
# which is why the constant is named for the asset and not shared implicitly.
USDT_DECIMALS = 6
TRX_DECIMALS = 6
_SCALE = Decimal(10) ** USDT_DECIMALS

# TronScan marks a native TRX transfer with this token name. Anything else on
# that endpoint is a TRC10 token, which is not what we are monitoring.
_TRX_TOKEN_NAME = "_"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Re-scan this far back each poll so boundary transactions are not missed.
OVERLAP_MS = 120_000  # two minutes

# Back off after repeated failures rather than hammering a rate-limited API.
# Capped so a wallet that has been failing all night is still retried on a
# sensible cadence rather than once an hour.
MAX_BACKOFF_SECONDS = 300
MAX_BACKOFF_CYCLES = 15

# Consecutive failures before the Bridge is told. Low enough to be useful,
# high enough that a single rate-limit blip does not raise an alarm — TronScan
# returns 429 under load and the TronGrid fallback usually absorbs it.
ESCALATE_AFTER_FAILURES = 5

# A TRON transaction confirms in well under a minute. Anything still
# unconfirmed after this is worth telling the Bridge about, because they may
# already have acted on the detection notice.
UNCONFIRMED_ALERT_MINUTES = 15

# How long a supplier's claim to have sent can go unanswered by an actual
# deposit before the Bridge is told. Chosen by the client on 11 September 2026:
# long enough that a slow confirmation is not reported as a no-show, short
# enough to still be actionable the same morning.
UNMATCHED_SEND_MINUTES = 30


def units_to_usdt(raw: Any) -> Decimal:
    """
    Convert the contract's integer units to USDT.

    Via str() so that a float never enters the ledger: Decimal(0.1) is
    0.1000000000000000055511151231257827, Decimal("0.1") is 0.1.
    """
    return Decimal(str(raw)) / _SCALE


def hex_to_base58(hex_address: str) -> str:
    """
    Convert TRON's hex address form (41...) to the base58check form (T...).

    TronGrid returns raw transactions with hex addresses while every other
    surface — TronScan, the database, the /wallet listing, the Bridge's own
    eyes — uses base58. Comparing the two forms directly silently matches
    nothing, which would look exactly like "no deposits arrived".
    """
    raw = bytes.fromhex(hex_address)
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    payload = raw + checksum

    number = int.from_bytes(payload, "big")
    out = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        out = _B58_ALPHABET[remainder] + out
    # Leading zero bytes are not representable in the arithmetic above and must
    # be restored as '1's, one per byte.
    for byte in payload:
        if byte != 0:
            break
        out = "1" + out
    return out


class TronClient:
    """Thin API client with an automatic fallback."""

    def __init__(
        self,
        *,
        tronscan_base: str,
        trongrid_base: str,
        trongrid_key: str,
        usdt_contract: str,
        asset: str = "USDT",
        timeout: float = 20.0,
    ):
        self.tronscan_base = tronscan_base.rstrip("/")
        self.trongrid_base = trongrid_base.rstrip("/")
        self.trongrid_key = trongrid_key
        self.usdt_contract = usdt_contract

        self.asset = asset.strip().upper()
        if self.asset not in ("USDT", "TRX"):
            raise ValueError(
                f"MONITOR_ASSET must be USDT or TRX, not {asset!r}. "
                "Refusing to start rather than monitoring the wrong thing."
            )
        if self.asset == "TRX":
            log.warning(
                "MONITOR_ASSET=TRX — monitoring native TRX, not USDT. "
                "This is a test setting. Production must be USDT."
            )

        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def incoming_transfers(
        self, address: str, since_ms: Optional[int]
    ) -> list[dict]:
        """
        Return incoming USDT transfers for `address`, newest last.

        Tries TronScan, falls back to TronGrid. If both fail the exception
        propagates so the caller can back off - returning an empty list here
        would look identical to "no deposits", which would silently hide a
        broken monitor.
        """
        if self.asset == "TRX":
            primary, fallback = self._tronscan_trx, self._trongrid_trx
        else:
            primary, fallback = self._tronscan, self._trongrid

        try:
            return await primary(address, since_ms)
        except Exception as exc:
            log.warning("TronScan failed for %s (%s), trying TronGrid", address, exc)
            return await fallback(address, since_ms)

    async def _tronscan(self, address: str, since_ms: Optional[int]) -> list[dict]:
        params: dict[str, Any] = {
            "relatedAddress": address,
            "limit": 50,
            "start": 0,
            "contract_address": self.usdt_contract,
            "sort": "-timestamp",
        }
        if since_ms:
            params["start_timestamp"] = max(0, since_ms - OVERLAP_MS)

        resp = await self._client.get(f"{self.tronscan_base}/token_trc20/transfers", params=params)
        resp.raise_for_status()
        payload = resp.json()

        out = []
        for item in payload.get("token_transfers", []):
            if (item.get("to_address") or "").strip() != address:
                continue  # outgoing, or an unrelated leg
            if item.get("finalResult") not in (None, "SUCCESS"):
                continue
            if item.get("revert"):
                # Live responses carry this flag. A reverted transfer moved no
                # money, so counting it would open a trade against a deposit
                # that never actually landed.
                continue
            out.append({
                "tx_hash": item.get("transaction_id") or item.get("hash"),
                "from_address": item.get("from_address"),
                "amount": units_to_usdt(item.get("quant", 0)),
                "timestamp_ms": int(item.get("block_ts") or item.get("block_timestamp") or 0),
                "block": item.get("block"),
                # B4: the client chose "notify on detection, confirm separately".
                # This is what separates the two.
                "confirmed": bool(item.get("confirmed")),
            })
        out.sort(key=lambda x: x["timestamp_ms"])
        return out

    async def _trongrid(self, address: str, since_ms: Optional[int]) -> list[dict]:
        headers = {"TRON-PRO-API-KEY": self.trongrid_key} if self.trongrid_key else {}
        params: dict[str, Any] = {
            "only_to": "true",
            "limit": 50,
            "contract_address": self.usdt_contract,
            "order_by": "block_timestamp,asc",
        }
        if since_ms:
            params["min_timestamp"] = max(0, since_ms - OVERLAP_MS)

        resp = await self._client.get(
            f"{self.trongrid_base}/v1/accounts/{address}/transactions/trc20",
            params=params, headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()

        out = []
        for item in payload.get("data", []):
            if (item.get("to") or "").strip() != address:
                continue
            out.append({
                "tx_hash": item.get("transaction_id"),
                "from_address": item.get("from"),
                "amount": units_to_usdt(item.get("value", 0)),
                "timestamp_ms": int(item.get("block_timestamp") or 0),
                "block": None,
                # This endpoint carries no confirmation flag. Treated as
                # unconfirmed rather than assumed good — the confirmation pass
                # will settle it, and guessing "confirmed" here would defeat
                # the whole point of the second check.
                "confirmed": False,
            })
        out.sort(key=lambda x: x["timestamp_ms"])
        return out

    # ------------------------------------------------------------- native TRX
    #
    # Test mode only. Same output shape as the TRC20 methods above, so nothing
    # downstream knows or cares which asset is being watched.

    async def _tronscan_trx(self, address: str, since_ms: Optional[int]) -> list[dict]:
        params: dict[str, Any] = {
            "address": address,
            "limit": 50,
            "start": 0,
            "sort": "-timestamp",
        }
        if since_ms:
            params["start_timestamp"] = max(0, since_ms - OVERLAP_MS)

        resp = await self._client.get(f"{self.tronscan_base}/transfer", params=params)
        resp.raise_for_status()
        payload = resp.json()

        out = []
        for item in payload.get("data", []):
            # This endpoint carries TRC10 tokens as well as native TRX. Only
            # TRX is money here; a TRC10 token with a similar amount would
            # otherwise open a trade against something worthless.
            if (item.get("tokenName") or "") != _TRX_TOKEN_NAME:
                continue
            if (item.get("transferToAddress") or "").strip() != address:
                continue
            if item.get("contractRet") not in (None, "SUCCESS"):
                continue
            if item.get("revert"):
                continue
            out.append({
                "tx_hash": item.get("transactionHash"),
                "from_address": item.get("transferFromAddress"),
                "amount": units_to_usdt(item.get("amount", 0)),
                "timestamp_ms": int(item.get("timestamp") or 0),
                "block": item.get("block"),
                "confirmed": bool(item.get("confirmed")),
            })
        out.sort(key=lambda x: x["timestamp_ms"])
        return out

    async def _trongrid_trx(self, address: str, since_ms: Optional[int]) -> list[dict]:
        """
        The fallback for TRX.

        TronGrid has no native-transfer endpoint, so this reads raw transactions
        and keeps the TransferContract ones. Addresses come back in hex and are
        converted before comparison — see hex_to_base58.
        """
        headers = {"TRON-PRO-API-KEY": self.trongrid_key} if self.trongrid_key else {}
        params: dict[str, Any] = {
            "only_to": "true",
            "limit": 50,
            "order_by": "block_timestamp,asc",
        }
        if since_ms:
            params["min_timestamp"] = max(0, since_ms - OVERLAP_MS)

        resp = await self._client.get(
            f"{self.trongrid_base}/v1/accounts/{address}/transactions",
            params=params, headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()

        out = []
        for item in payload.get("data", []):
            rets = item.get("ret") or []
            if rets and rets[0].get("contractRet") not in (None, "SUCCESS"):
                continue

            contracts = (item.get("raw_data") or {}).get("contract") or []
            if not contracts or contracts[0].get("type") != "TransferContract":
                # Staking, delegation, contract calls — everything that is not
                # someone sending TRX to this address.
                continue

            value = (contracts[0].get("parameter") or {}).get("value") or {}
            to_hex = value.get("to_address")
            if not to_hex or hex_to_base58(to_hex) != address:
                continue

            from_hex = value.get("owner_address")
            out.append({
                "tx_hash": item.get("txID"),
                "from_address": hex_to_base58(from_hex) if from_hex else None,
                "amount": units_to_usdt(value.get("amount", 0)),
                "timestamp_ms": int(item.get("block_timestamp") or 0),
                "block": item.get("blockNumber"),
                "confirmed": False,      # same reasoning as the TRC20 fallback
            })
        out.sort(key=lambda x: x["timestamp_ms"])
        return out


class DepositMonitor:
    def __init__(self, *, repo, client: TronClient, notifier, config):
        self.repo = repo
        self.client = client
        self.notifier = notifier
        self.config = config

        # Consecutive FAILED ATTEMPTS per wallet, and cycles skipped since the
        # last attempt. Two counters on purpose: one variable serving both
        # meant the escalation threshold below sat on a count that only ever
        # advanced while skipping, so it was never reached during an attempt
        # and the Bridge was never told.
        self._failures: dict[int, int] = {}
        self._skips: dict[int, int] = {}

    async def run_forever(self) -> None:
        log.info(
            "deposit monitor starting, polling every %ss",
            self.config.poll_interval_seconds,
        )
        while True:
            try:
                await self.poll_once(stagger=True)
            except Exception:
                # A crash here would stop deposit detection silently, which is
                # the worst failure this system has. Always log and continue.
                log.exception("monitor cycle failed")
                await asyncio.sleep(self.config.poll_interval_seconds)

    async def poll_once(self, *, stagger: bool = False) -> None:
        """
        One pass over every monitored wallet.

        With stagger=True the calls are spread evenly across the poll interval
        rather than fired in a burst and then sleeping. Same number of requests
        per minute either way, but the providers care about bursts: TronScan
        returned 429 on the fourth call in a few seconds on 9 September 2026,
        while the same volume spread out went through untouched.

        This matters more the shorter the interval gets. At five seconds with
        several wallets, bursting would sit permanently in rate-limit territory
        and push every poll onto the fallback provider.
        """
        # Re-read the wallet list every cycle rather than caching it, so a
        # /walletchange takes effect on the next pass with no restart.
        wallets = await self.repo.monitored_wallets()

        if not wallets:
            # Nothing to watch. Without this the staggered loop would spin.
            if stagger:
                await asyncio.sleep(self.config.poll_interval_seconds)
            return

        gap = self.config.poll_interval_seconds / len(wallets) if stagger else 0

        for wallet in wallets:
            await self._poll_wallet(wallet)
            if gap:
                await asyncio.sleep(gap)

        await self._report_unconfirmed()
        await self._report_unmatched_sends()

    async def _report_unmatched_sends(self) -> None:
        """
        A supplier said they had sent, and nothing arrived.

        The Bridge is no longer told the moment a supplier claims to have sent
        — they are told when funds land (client request, 11 September 2026).
        The cost of that is a claim which never materialises would simply
        vanish, so it is reported here instead.

        Each is reported once. The claim itself is a fact worth keeping even
        after it is reported, so the row stays and is only marked.
        """
        try:
            stale = await self.repo.stale_pending_sends(UNMATCHED_SEND_MINUTES)
        except Exception:
            log.exception("could not check for unmatched sends")
            return

        for row in stale:
            # Claim it first: the alert must not be sent twice if a poll
            # overlaps, and losing one alert is better than repeating it.
            if not await self.repo.mark_pending_alerted(row["id"]):
                continue

            account = row["account_name"] or "an unnamed account"
            await self.notifier.to_bridge(
                f"{row['supplier_label']} said they had sent "
                f"{UNMATCHED_SEND_MINUTES} minutes ago, but no deposit has "
                f"arrived.\n\n"
                f"They nominated: {account}\n"
                + (f"Hash they gave: {row['hash_url']}\n" if row["hash_url"] else "")
                + "\nNothing has been opened. Worth checking with them."
            )
            log.warning(
                "unmatched send from %s after %s minutes",
                row["supplier_label"], UNMATCHED_SEND_MINUTES,
            )

    async def _report_unconfirmed(self) -> None:
        """
        Tell the Bridge about deposits that never confirmed.

        The Bridge is notified the moment a deposit is detected, which is what
        the client asked for — but that means they can act on a transaction
        that has not settled. On TRON this is rare and usually means the
        transaction was dropped. Either way it should not stay silent.

        Each one is reported once; the status moves off 'detected' so the next
        cycle does not repeat it.
        """
        try:
            stale = await self.repo.unconfirmed_deposits(UNCONFIRMED_ALERT_MINUTES)
        except Exception:
            log.exception("could not check for unconfirmed deposits")
            return

        for d in stale:
            if not await self.repo.flag_unconfirmed(d["id"]):
                continue  # another pass got there first
            log.warning("deposit %s still unconfirmed", d["tx_hash"])
            await self.notifier.to_bridge(
                "DEPOSIT NOT CONFIRMED\n\n"
                f"{d['amount_usdt']} USDT to {d['address']}\n"
                f"Hash {d['tx_hash']}\n"
                + (f"Trade {d['reference']}\n" if d["reference"] else "")
                + f"\nDetected over {UNCONFIRMED_ALERT_MINUTES} minutes ago and "
                "still not confirmed on-chain. Check the transaction before "
                "acting on it."
            )

    async def _poll_wallet(self, wallet) -> None:
        wallet_id = wallet["id"]

        # Back off a failing wallet so one bad address does not exhaust the API
        # budget for the others: skip as many cycles as there have been
        # consecutive failures, up to the cap.
        failures = self._failures.get(wallet_id, 0)
        if failures:
            skips = self._skips.get(wallet_id, 0)
            if skips < min(failures, MAX_BACKOFF_CYCLES):
                self._skips[wallet_id] = skips + 1
                return
            self._skips[wallet_id] = 0

        try:
            transfers = await self.client.incoming_transfers(
                wallet["address"], wallet["last_timestamp_ms"]
            )
        except Exception as exc:
            failures += 1
            self._failures[wallet_id] = failures
            log.error(
                "both APIs failed for wallet %s (%s), %s consecutive: %s",
                wallet_id, wallet["address"], failures, exc,
            )
            # Once when it becomes a real outage rather than a blip, then
            # periodically — a single message hours ago is easy to miss, and a
            # wallet that is no longer being polled must not go quiet.
            if failures == ESCALATE_AFTER_FAILURES or failures % 20 == 0:
                await self.notifier.to_bridge(
                    f"Wallet monitoring is failing for {wallet['address']}. "
                    f"{failures} consecutive failures. "
                    "Deposits may not be detected. Please check."
                )
            return

        if failures:
            log.info("wallet %s recovered after %s failures", wallet_id, failures)
        self._failures.pop(wallet_id, None)
        self._skips.pop(wallet_id, None)

        # ADOPTION. A wallet with no cursor is one we have only just been told
        # to watch — newly added, or an address just changed. It is NOT a wallet
        # whose history we are behind on.
        #
        # Without this the first poll has no lower bound, so the provider
        # returns the most recent transactions that already exist and every one
        # is treated as a deposit that just landed. On 9 September 2026 that
        # turned two of the client's live wallets into 38 deposits and a trade
        # for 188,167 USDT against ₹19,851,619, from transfers dating back to
        # June. On go-live, with real wallets, it would have done the same in
        # front of the client.
        #
        # So the first pass adopts the wallet at its current state and records
        # nothing. The cursor is set from the newest transaction the chain
        # already shows rather than from this server's clock, so it does not
        # depend on the clock being right.
        if wallet["last_timestamp_ms"] is None:
            newest = max((t["timestamp_ms"] for t in transfers), default=0)

            # The + OVERLAP_MS + 1 is not padding, it is the whole point.
            #
            # Every poll deliberately rewinds the cursor by OVERLAP_MS, because
            # block timestamps are not perfectly ordered. During normal running
            # that is free: anything re-read is absorbed by the UNIQUE
            # (tx_hash, wallet_id) constraint. Adoption is the one case where it
            # is not free, because adopted transactions are deliberately NOT
            # recorded — there is no row for the constraint to collide with.
            #
            # So a cursor set to `newest` is rewound to `newest - OVERLAP_MS` on
            # the very next poll, and the transaction we just decided to ignore
            # comes straight back as a new deposit. That happened: a 30 TRX
            # transfer from 25 August 2026 was reported as arriving on 10
            # September, fifteen days later.
            #
            # Offsetting by the overlap makes the next query start at
            # `newest + 1`, which excludes it. Nothing real is lost — a genuine
            # deposit arrives after adoption, so its timestamp is far beyond
            # `newest`, and the overlap still protects every poll after this one.
            # The offset is an optimisation — it keeps the provider from
            # returning history we would only throw away. It is NOT the
            # guarantee. TronScan truncates start_timestamp to whole seconds,
            # so a millisecond boundary does not hold, and asking from
            # ...471001 still returns the transaction at ...471000. The
            # guarantee is adopted_at_ms, enforced below in our own code.
            if newest:
                cursor = newest + OVERLAP_MS + 1
            else:
                newest = int(time.time() * 1000)
                cursor = newest

            await self.repo.adopt_wallet(
                wallet_id, cursor_ms=cursor, adopted_at_ms=newest
            )

            log.warning(
                "adopted wallet %s (%s): %s existing transaction(s) ignored, "
                "cursor set to %s",
                wallet_id, wallet["address"], len(transfers), cursor,
            )
            # Said out loud, because "the bot ignored what was already there"
            # is exactly the kind of decision that must not be silent.
            await self.notifier.to_bridge(
                f"Now monitoring {wallet['address']}\n\n"
                + (f"{len(transfers)} existing transaction(s) on this address "
                   "were ignored. Only new deposits from now on will be picked up."
                   if transfers else
                   "No existing transactions. Ready.")
            )
            return

        # The adoption boundary, enforced here rather than trusted to the
        # provider's date filter. Everything at or before it existed before we
        # were asked to watch this wallet, and adoption deliberately recorded
        # none of it — so there is no database row to absorb a re-read, and the
        # two-minute overlap would otherwise walk straight back into it on
        # every single poll.
        adopted_at = wallet["adopted_at_ms"]

        for tr in transfers:
            if not tr["tx_hash"] or tr["amount"] <= 0:
                continue

            if adopted_at is not None and tr["timestamp_ms"] <= adopted_at:
                log.debug(
                    "skipping pre-adoption transaction %s on wallet %s",
                    tr["tx_hash"], wallet_id,
                )
                continue

            # Dust. TRON wallets receive unsolicited micro-transfers constantly,
            # usually address-poisoning: the sender's address is crafted to look
            # like one you have used before, hoping it gets copied out of your
            # history later. They are not deposits. Without this floor each one
            # opens a trade, burns a reference number, and puts a notification
            # in front of the Bridge for a fraction of a rupee.
            #
            # Skipped quietly and logged rather than announced — announcing it
            # would simply move the noise from one channel to another.
            if tr["amount"] < self.config.min_deposit_amount:
                log.info(
                    "ignoring dust deposit of %s to wallet %s (tx %s)",
                    tr["amount"], wallet_id, tr["tx_hash"],
                )
                continue

            await self._handle_deposit(wallet, tr)

        # The cursor advances past dust as well, so ignored transfers are not
        # re-examined on every subsequent poll.
        if transfers:
            await self.repo.set_monitor_cursor(
                wallet_id, max(t["timestamp_ms"] for t in transfers)
            )

    async def _handle_deposit(self, wallet, transfer: dict) -> None:
        deposit_id = await self.repo.record_deposit(
            tx_hash=transfer["tx_hash"],
            wallet_id=wallet["id"],
            amount_usdt=transfer["amount"],
            from_address=transfer["from_address"],
            block_number=transfer["block"],
            confirmed=transfer.get("confirmed", False),
        )

        if deposit_id is None:
            # Already recorded. Normal after a restart or an overlapping window
            # — and the opportunity to complete B4's second half: if the chain
            # now reports it confirmed, promote it.
            if transfer.get("confirmed"):
                promoted = await self.repo.confirm_deposit(
                    tx_hash=transfer["tx_hash"],
                    wallet_id=wallet["id"],
                    block_number=transfer["block"],
                )
                if promoted:
                    log.info("deposit %s confirmed on-chain", transfer["tx_hash"])
            return

        log.info(
            "new deposit %s USDT to wallet %s (tx %s)",
            transfer["amount"], wallet["id"], transfer["tx_hash"],
        )

        if not wallet["is_internal"]:
            # B5: a deposit on a wallet that is not a supplier→client pairing
            # has no trade to attach to, so no trade is opened.
            #
            # It is still worth telling the owner of that wallet, and this is
            # the ordinary case rather than an oddity: it is the Bridge sending
            # the client their USDT. The client was previously left to notice
            # the arrival on their own (client request, 10 September 2026:
            # "i just sent money onto client account but bot did not notify
            # client of hash?").
            # Say what it is, not what it is not.
            #
            # This read "Deposit detected on a non-internal wallet ... No
            # trade was opened", which describes a failure. It is not one:
            # it is the Bridge's own settlement arriving in a counterparty's
            # wallet, which is the last step of a trade working correctly.
            #
            # On 11 September 2026 the Bridge saw it against his own 13,992.59
            # payout for SUPA3 and asked "no trade was opened?" — reasonably,
            # because the wording invites exactly that question at the moment
            # he is least able to absorb another alarm.
            owner = wallet["owner_party_id"]
            owner_label = await self.repo.party_label(owner) if owner else None
            whose = f"{owner_label}'s" if owner_label else "a counterparty's"

            await self.notifier.to_bridge(
                f"Onward payout confirmed on chain\n"
                f"{transfer['amount']} USDT reached {whose} wallet\n"
                f"Hash {tx_link(transfer['tx_hash'], html=True)}\n"
                f"Nothing to action — a settlement leaving the desk is not a "
                f"supplier deposit, so no trade is opened for it.",
                html=True,
            )

            if owner:
                await self.notifier.to_party(
                    owner,
                    render_payout_notice(
                        amount=transfer["amount"],
                        tx_hash=transfer["tx_hash"],
                        html=True,
                    ),
                    html=True,
                )
            return

        await self.notifier.on_supplier_deposit(
            wallet=wallet,
            deposit_id=deposit_id,
            amount_usdt=transfer["amount"],
            tx_hash=transfer["tx_hash"],
        )
