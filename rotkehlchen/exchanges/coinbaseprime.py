import base64
import hashlib
import hmac
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, Literal, overload
from urllib.parse import urlparse

import requests

from rotkehlchen.accounting.structures.balance import Balance
from rotkehlchen.assets.converters import asset_from_coinbase
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.errors.asset import UnknownAsset, UnsupportedAsset
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.exchanges.data_structures import AssetMovement, Fee, MarginPosition, Trade
from rotkehlchen.exchanges.exchange import ExchangeInterface, ExchangeQueryBalances
from rotkehlchen.inquirer import Inquirer, Price
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.serialization.deserialize import (
    deserialize_asset_amount,
    deserialize_asset_amount_force_positive,
    deserialize_fee,
    deserialize_fval,
)
from rotkehlchen.types import (
    ApiKey,
    ApiSecret,
    AssetMovementCategory,
    ExchangeAuthCredentials,
    Location,
    Timestamp,
    TradeType,
)
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.misc import iso8601ts_to_timestamp, timestamp_to_iso8601, ts_now
from rotkehlchen.utils.mixins.cacheable import cache_response_timewise
from rotkehlchen.utils.mixins.lockable import protect_with_lock

if TYPE_CHECKING:
    from rotkehlchen.assets.asset import AssetWithOracles
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.history.events.structures.base import HistoryEvent


PRIME_BASE_URL: Final = 'https://api.prime.coinbase.com/v1'
COMPLETED_TRANSACTION_STATUS: Final = {
    'TRANSACTION_DONE',
    'TRANSACTION_IMPORTED',
}
logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


def _process_trade(trade_data: dict[str, Any]) -> Trade | None:
    """Process trade from coinbase prime. Returns None if the order can't be processed
    May raise:
    - DeserializationError
    """
    try:
        if trade_data['status'] != 'FILLED':
            return None

        if not isinstance(trade_data['product_id'], str) and '-' not in trade_data['product_id']:
            raise DeserializationError(
                f'Found a not valid product_id {trade_data["product_data"]}',
            )

        if len(pair_data := trade_data['product_id'].split('-')) != 2:
            raise DeserializationError(f'Non valid product found in trade {trade_data}')

        try:
            base_asset = asset_from_coinbase(pair_data[0])
            quote_asset = asset_from_coinbase(pair_data[1])
            trade_type = TradeType.deserialize(trade_data['side'])
        except UnknownAsset as e:
            raise DeserializationError(
                f'Unknown asset {e.identifier} seen in coinbase prime trade',
            ) from e

        if trade_type == TradeType.BUY:
            amount = deserialize_asset_amount(trade_data['filled_quantity'])
            rate = deserialize_fval(
                value=trade_data['net_average_filled_price'],
                name='rate',
                location='coinbase prime',
            )  # filled_quantity * rate == quote_value
        else:
            amount = deserialize_asset_amount(trade_data['filled_value'])
            rate = deserialize_fval(
                value=trade_data['average_filled_price'],
                name='rate',
                location='coinbase prime',
            )  # filled_quantity * average_filled_price == filled_value

        return Trade(
            timestamp=iso8601ts_to_timestamp(trade_data['created_at']),
            location=Location.COINBASEPRIME,
            base_asset=base_asset,
            quote_asset=quote_asset,
            trade_type=trade_type,
            amount=amount,
            rate=Price(rate),
            fee=deserialize_fee(trade_data['commission']) if len(trade_data['commission']) != 0 else Fee(ZERO),  # noqa: E501
            fee_currency=quote_asset,
            link=str(trade_data['id']),
        )
    except KeyError as e:
        raise DeserializationError(
            f'Missing key {e} in trade information for Coinbase Prime',
        ) from e


def _process_deposit_withdrawal(event_data: dict[str, Any]) -> AssetMovement | None:
    """Process asset movement from coinbase prime. Returns None if the event can't be processed
    May raise:
    - DeserializationError
    """
    try:
        if event_data['status'] not in COMPLETED_TRANSACTION_STATUS:
            return None

        event_type = AssetMovementCategory.deserialize(event_data['type'])
        if event_type == AssetMovementCategory.DEPOSIT:
            address = event_data.get('transfer_from', {}).get('value')
        else:
            address = event_data.get('transfer_to', {}).get('value')

        amount = deserialize_asset_amount_force_positive(event_data['amount'])
        fee = deserialize_fee(event_data['fees'])
        timestamp = iso8601ts_to_timestamp(event_data['completed_at'])
        try:
            fee_asset = asset_from_coinbase(event_data['fee_symbol'])
            asset = asset_from_coinbase(event_data['symbol'])
        except UnknownAsset as e:
            raise DeserializationError(
                f'Unknown asset {e.identifier} seen in coinbase prime trade',
            ) from e

        return AssetMovement(
            location=Location.COINBASEPRIME,
            category=event_type,
            address=address,
            transaction_id=event_data['blockchain_ids'][0] if len(event_data['blockchain_ids']) != 0 else None,  # noqa: E501
            timestamp=timestamp,
            asset=asset,
            amount=amount,
            fee=fee,
            fee_asset=fee_asset,
            link=event_data['id'],
        )
    except KeyError as e:
        raise DeserializationError(
            f'Missing key {e} in asset movement information for Coinbase Prime',
        ) from e


class Coinbaseprime(ExchangeInterface):

    def __init__(
            self,
            name: str,
            api_key: ApiKey,
            secret: ApiSecret,
            database: 'DBHandler',
            msg_aggregator: MessagesAggregator,
            passphrase: str,
    ):
        super().__init__(
            name=name,
            location=Location.COINBASEPRIME,
            api_key=api_key,
            secret=secret,
            database=database,
            msg_aggregator=msg_aggregator,
        )
        self.api_passphrase = passphrase
        self.session.headers.update({
            'Content-Type': 'application/json',
            'X-CB-ACCESS-KEY': self.api_key,
            'X-CB-ACCESS-PASSPHRASE': self.api_passphrase,
        })

    def validate_api_key(self) -> tuple[bool, str]:
        try:
            self._get_portfolio_ids()
        except RemoteError as e:
            return False, str(e)
        else:
            return True, ''

    def update_passphrase(self, new_passphrase: str) -> None:
        self.api_passphrase = new_passphrase

    def edit_exchange_credentials(self, credentials: 'ExchangeAuthCredentials') -> bool:
        if super().edit_exchange_credentials(credentials) is False:
            return False

        if credentials.api_key is not None:
            self.session.headers.update({'X-CB-ACCESS-KEY': self.api_key})
        if credentials.passphrase is not None:
            self.api_passphrase = credentials.passphrase

        return True

    def sign(self, timestamp: Timestamp, url_path: str) -> bytes:
        """Sign requests for coinbase prime"""
        message = f'{timestamp}GET{url_path}'
        hmac_message = hmac.digest(self.secret, message.encode(), hashlib.sha256)
        return base64.b64encode(hmac_message)

    def _api_query(
            self,
            module: Literal['portfolios'],
            path: str = '',
            params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        uri = f'{PRIME_BASE_URL}/{module}'
        if path != '':
            uri += f'/{path}'

        url_path = urlparse(uri).path
        self.session.headers.update({
            'X-CB-ACCESS-TIMESTAMP': str(timestamp := ts_now()),
            'X-CB-ACCESS-SIGNATURE': self.sign(timestamp, url_path),
        })
        log.debug(f'Querying coinbase prime module {module}/{path} with {params=}')
        try:
            response = self.session.get(url=uri, params=params)
        except requests.RequestException as e:
            raise RemoteError(f'Coinbase Prime API request failed due to {e}') from e

        try:
            data = response.json()
        except requests.JSONDecodeError as e:
            raise RemoteError(f'Coinbase Prime returned invalid json {response.text}') from e

        return data

    @overload
    def _query_paginated_endpoint(
            self,
            query_params: dict[str, Any],
            portfolio_id: str,
            method: Literal['transactions'],
            decoding_logic: Callable[[dict[str, Any]], AssetMovement | None],
    ) -> list[AssetMovement]:
        ...

    @overload
    def _query_paginated_endpoint(
            self,
            query_params: dict[str, Any],
            portfolio_id: str,
            method: Literal['orders'],
            decoding_logic: Callable[[dict[str, Any]], Trade | None],
    ) -> list[Trade]:
        ...

    def _query_paginated_endpoint(
            self,
            query_params: dict[str, Any],
            portfolio_id: str,
            method: Literal['orders', 'transactions'],
            decoding_logic: Callable[[dict[str, Any]], AssetMovement | None] | Callable[[dict[str, Any]], Trade | None],  # noqa: E501
    ) -> list[Trade] | list[AssetMovement]:
        """Abstraction to consume all the events in the selected queries.
        It uses the `decoding_logic` to process the different events and returns a list
        whose contents depend on the function's called arguments.

        This function may raise:
        - RemoteError
        """
        result = []
        while True:
            response = self._api_query(
                module='portfolios',
                path=f'{portfolio_id}/{method}',
                params=query_params,
            )

            for raw_event in response[method]:
                try:
                    if (event := decoding_logic(raw_event)) is None:
                        log.warning(
                            f'Wont process event {raw_event} from coinbase prime. Skipping',
                        )
                    else:
                        result.append(event)
                except DeserializationError as e:
                    self.msg_aggregator.add_error(
                        f'Failed to process coinbase prime event due to {e}. Skipping entry...',
                    )

            if (
                response['pagination']['has_next'] is True and
                (new_cursor := response['pagination'].get('next_cursor', '')) != ''
            ):
                query_params['cursor'] = new_cursor
            else:
                break

        return result  # type: ignore  # mypy doesn't detect that the return type here is defined by the function used

    def _get_portfolio_ids(self) -> list[str]:
        """
        Get id of the different portfolios linked to the api keys.
        May raise: RemoteError
        """
        data: dict[str, list] = self._api_query(module='portfolios')
        try:
            ids = [portfolio['id'] for portfolio in data['portfolios']]
        except KeyError as e:
            log.error(
                f'Malformed portfolios response from coinbase prime {data}. Missing key {e}',
            )
            raise RemoteError(
                'Malformed porfolios ids response in coinbase prime. Check logs for more details',
            ) from e

        return ids

    @protect_with_lock()
    @cache_response_timewise()
    def query_balances(self) -> ExchangeQueryBalances:
        try:
            portfolio_ids = self._get_portfolio_ids()
        except RemoteError as e:
            msg_prefix = 'Coinbase Prime API request failed.'
            msg = (
                'Coinbase Prime API request failed. Could not reach coinbase due '
                f'to {e}'
            )
            log.error(f'{msg_prefix} Could not reach coinbase due to {e}')
            return None, f'{msg_prefix} Check logs for more details'

        returned_balances: defaultdict[AssetWithOracles, Balance] = defaultdict(Balance)
        for account_id in portfolio_ids:
            try:
                balances_query: dict[str, list[dict[str, Any]]] = self._api_query(
                    module='portfolios',
                    path=f'{account_id}/balances',
                )
            except RemoteError as e:
                log.error(f'Failed to query CoinbasePrime balances due to {e}')
                return None, 'Request to CoinbasePrime failed to fetch balances'

            for balance_entry in balances_query['balances']:
                try:
                    total_balance = ZERO
                    for balance_key in (
                        'amount',  # The `amount` field includes the amount in the `holds` field
                        'bonded_amount',
                        'reserved_amount'  # Amount that must remain in the wallet due to the protocol, in whole units  # noqa: E501
                        'unbonding_amount',
                        'unvested_amount',
                        'pending_rewards_amount',
                    ):
                        total_balance += deserialize_asset_amount(
                            amount=balance_entry.get(balance_key, ZERO),
                        )

                    # ignore empty balances. Coinbase returns zero balances for everything
                    # a user does not own
                    if total_balance == ZERO:
                        continue

                    asset = asset_from_coinbase(balance_entry['symbol'])
                    try:
                        usd_price = Inquirer.find_usd_price(asset=asset)
                    except RemoteError as e:
                        log.error(
                            f'Error processing coinbase balance entry due to inability to '
                            f'query USD price: {e!s}. Skipping balance entry',
                        )
                        continue

                    returned_balances[asset] += Balance(
                        amount=total_balance,
                        usd_value=total_balance * usd_price,
                    )
                except UnknownAsset as e:
                    self.send_unknown_asset_message(
                        asset_identifier=e.identifier,
                        details='balance query',
                    )
                except UnsupportedAsset as e:
                    log.warning(
                        f'Found coinbase prime balance result with unsupported asset '
                        f'{e.identifier}. Ignoring it.',
                    )
                except (DeserializationError, KeyError) as e:
                    msg = str(e)
                    if isinstance(e, KeyError):
                        msg = f'Missing key entry for {msg}.'
                    log.error(
                        'Error processing a coinbase prime account balance',
                        account_balance=account_id,
                        error=msg,
                    )

        return dict(returned_balances), ''

    def query_online_income_loss_expense(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> list['HistoryEvent']:
        return []

    def query_online_deposits_withdrawals(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> list['AssetMovement']:
        """
        May raise:
        - RemoteError if we can't query coinbase
        """
        portfolio_ids = self._get_portfolio_ids()
        movements = []
        for portfolio_id in portfolio_ids:
            query_params = {
                'sort_direction': 'ASC',
                'start_time': timestamp_to_iso8601(start_ts),
                'end_time': timestamp_to_iso8601(end_ts),
                'types': ['DEPOSIT', 'WITHDRAWAL', 'COINBASE_DEPOSIT'],
            }
            movements.extend(
                self._query_paginated_endpoint(
                    query_params=query_params,
                    portfolio_id=portfolio_id,
                    method='transactions',
                    decoding_logic=_process_deposit_withdrawal,
                ),
            )

        return movements

    def query_online_margin_history(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> list[MarginPosition]:
        return []

    def query_online_trade_history(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> tuple[list[Trade], tuple[Timestamp, Timestamp]]:
        """
        May raise:
        - RemoteError: if we can't query coinbase
        """
        portfolio_ids = self._get_portfolio_ids()
        trades = []
        for portfolio_id in portfolio_ids:
            query_params = {
                'sort_direction': 'ASC',
                'order_statuses': ['FILLED'],
                'start_date': timestamp_to_iso8601(start_ts),
                'end_date': timestamp_to_iso8601(end_ts),
            }
            trades.extend(
                self._query_paginated_endpoint(
                    query_params=query_params,
                    portfolio_id=portfolio_id,
                    method='orders',
                    decoding_logic=_process_trade,
                ),
            )

        return trades, (start_ts, end_ts)

    def first_connection(self) -> None:
        return None