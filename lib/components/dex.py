from datetime import datetime
import logging
import decimal
import base64
import json
import time

from lib import config, util

decimal.setcontext(decimal.Context(prec=8, rounding=decimal.ROUND_HALF_EVEN))
D = decimal.Decimal

def calculate_price(base_quantity, quote_quantity, base_divisibility, quote_divisibility):
    if not base_divisibility:
        base_quantity *= config.UNIT
    if not quote_divisibility:
        quote_quantity *= config.UNIT

    try:
        return float(quote_quantity) / float(base_quantity)
    except Exception, e:
        return 0

def format_price(base_quantity, quote_quantity, base_divisibility, quote_divisibility):
    price = calculate_price(base_quantity, quote_quantity, base_divisibility, quote_divisibility)
    return format(price, '.8f')

def get_pairs_with_orders(addresses=[], max_pairs=12):

    pairs_with_orders = []

    sources = '''AND source IN ({})'''.format(','.join(['?' for e in range(0,len(addresses))]))

    sql = '''SELECT (MIN(give_asset, get_asset) || '/' || MAX(give_asset, get_asset)) AS pair,
                    COUNT(*) AS order_count
             FROM orders
             WHERE give_asset != get_asset AND status = ? {}
             GROUP BY pair
             ORDER BY order_count DESC
             LIMIT ?'''.format(sources)

    bindings = ['open'] + addresses + [max_pairs]

    my_pairs = util.call_jsonrpc_api('sql', {'query': sql, 'bindings': bindings})['result']

    for my_pair in my_pairs:
        base_asset, quote_asset = util.assets_to_asset_pair(*tuple(my_pair['pair'].split("/")))
        top_pair = {
            'base_asset': base_asset,
            'quote_asset': quote_asset,
            'my_order_count': my_pair['order_count']
        }
        if my_pair['pair'] == 'SCH/SHP': # SHP/SCH always in first
            pairs_with_orders.insert(0, top_pair)
        else:
            pairs_with_orders.append(top_pair)

    return pairs_with_orders


def get_xcp_or_btc_pairs(asset='SHP', exclude_pairs=[], max_pairs=12, from_time=None):

    bindings = []

    sql = '''SELECT (CASE
                        WHEN forward_asset = ? THEN backward_asset
                        ELSE forward_asset
                    END) AS base_asset,
                    (CASE
                        WHEN backward_asset = ? THEN backward_asset
                        ELSE forward_asset
                    END) AS quote_asset,
                    (CASE
                        WHEN backward_asset = ? THEN (forward_asset || '/' || backward_asset)
                        ELSE (backward_asset || '/' || forward_asset)
                    END) AS pair,
                    (CASE
                        WHEN forward_asset = ? THEN SUM(backward_quantity)
                        ELSE SUM(forward_quantity)
                    END) AS base_quantity,
                    (CASE
                        WHEN backward_asset = ? THEN SUM(backward_quantity)
                        ELSE SUM(forward_quantity)
                    END) AS quote_quantity '''
    if from_time:
        sql += ''', block_time '''

    sql += '''FROM order_matches '''
    bindings += [asset, asset, asset, asset, asset]

    if from_time:
        sql += '''INNER JOIN blocks ON order_matches.block_index = blocks.block_index '''

    if asset == 'SHP':
        sql += '''WHERE ((forward_asset = ? AND backward_asset != ?) OR (forward_asset != ? AND backward_asset = ?)) '''
        bindings += [asset, 'SCH', 'SCH', asset]
    else:
        sql += '''WHERE ((forward_asset = ?) OR (backward_asset = ?)) '''
        bindings += [asset, asset]

    if len(exclude_pairs) > 0:
        sql += '''AND pair NOT IN ({}) '''.format(','.join(['?' for e in range(0,len(exclude_pairs))]))
        bindings += exclude_pairs

    if from_time:
        sql += '''AND block_time > ? '''
        bindings += [from_time]

    sql += '''AND forward_asset != backward_asset
              GROUP BY pair
              ORDER BY quote_quantity DESC
              LIMIT ?'''
    bindings += [max_pairs]

    return util.call_jsonrpc_api('sql', {'query': sql, 'bindings': bindings})['result']


def get_xcp_and_btc_pairs(exclude_pairs=[], max_pairs=12, from_time=None):

    all_pairs = []

    for currency in ['SHP', 'SCH']:
        currency_pairs = get_xcp_or_btc_pairs(asset=currency, exclude_pairs=exclude_pairs, max_pairs=max_pairs, from_time=from_time)
        for currency_pair in currency_pairs:
            if currency_pair['pair'] == 'SHP/SCH':
                all_pairs.insert(0, currency_pair)
            else:
                all_pairs.append(currency_pair)

    return all_pairs

def get_users_pairs(addresses=[], max_pairs=12):

    top_pairs = []
    all_assets = []
    exclude_pairs = []

    if len(addresses) > 0:
        top_pairs += get_pairs_with_orders(addresses, max_pairs)

    for p in top_pairs:
        exclude_pairs += [p['base_asset'] + '/' + p['quote_asset']]
        all_assets += [p['base_asset'], p['quote_asset']]

    for currency in ['SHP', 'SCH']:
        if len(top_pairs) < max_pairs:
            limit = max_pairs - len(top_pairs)
            currency_pairs = get_xcp_or_btc_pairs(currency, exclude_pairs, limit)
            for currency_pair in currency_pairs:
                top_pair = {
                    'base_asset': currency_pair['base_asset'],
                    'quote_asset': currency_pair['quote_asset']
                }
                if currency_pair['pair'] == 'SHP/SCH': # SHP/SCH always in first
                    top_pairs.insert(0, top_pair)
                else:
                    top_pairs.append(top_pair)
                all_assets += [currency_pair['base_asset'], currency_pair['quote_asset']]

    if 'SHP/SCH' not in [p['base_asset'] + '/' + p['quote_asset'] for p in top_pairs]:
        top_pairs.insert(0, {
            'base_asset': 'SHP',
            'quote_asset': 'SCH'
        })
        all_assets += ['SHP', 'SCH']

    top_pairs = top_pairs[:12]
    all_assets = list(set(all_assets))
    supplies = get_assets_supply(all_assets)

    for p in range(len(top_pairs)):
        price, trend, price24h, progression = get_price_movement(top_pairs[p]['base_asset'], top_pairs[p]['quote_asset'], supplies=supplies)
        top_pairs[p]['price'] = format(price, ".8f")
        top_pairs[p]['trend'] = trend
        top_pairs[p]['progression'] = format(progression, ".2f")
        top_pairs[p]['price_24h'] = format(price24h, ".8f")

    return top_pairs


def get_market_orders(asset1, asset2, addresses=[], supplies=None, min_fee_provided=0.95, max_fee_required=0.95):

    base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
    if not supplies:
        supplies = get_assets_supply([asset1, asset2])
    market_orders = []

    sql = '''SELECT orders.*, blocks.block_time FROM orders INNER JOIN blocks ON orders.block_index=blocks.block_index
             WHERE  status = ? '''
    bindings = ['open']

    if len(addresses) > 0:
        sql += '''AND source IN ({}) '''.format(','.join(['?' for e in range(0,len(addresses))]))
        bindings += addresses

    sql += '''AND give_remaining > 0
              AND give_asset IN (?, ?)
              AND get_asset IN (?, ?)
              ORDER BY tx_index DESC'''

    bindings +=  [asset1, asset2, asset1, asset2]

    orders = util.call_jsonrpc_api('sql', {'query': sql, 'bindings': bindings})['result']

    for order in orders:
        user_order = {}

        exclude = False
        if order['give_asset'] == 'SCH':
            try:
                fee_provided = order['fee_provided'] / (order['give_quantity'] / 100)
                user_order['fee_provided'] = format(D(order['fee_provided']) / (D(order['give_quantity']) / D(100)), '.2f')
            except Exception, e:
                fee_provided = min_fee_provided - 1 # exclude

            exclude = fee_provided < min_fee_provided

        elif order['get_asset'] == 'SCH':
            try:
                fee_required = order['fee_required'] / (order['get_quantity'] / 100)
                user_order['fee_required'] = format(D(order['fee_required']) / (D(order['get_quantity']) / D(100)), '.2f')
            except Exception, e:
                fee_required = max_fee_required + 1 # exclude

            exclude = fee_required > max_fee_required


        if not exclude:
            if order['give_asset'] == base_asset:
                price = calculate_price(order['give_quantity'], order['get_quantity'], supplies[order['give_asset']][1], supplies[order['get_asset']][1])
                user_order['type'] = 'SELL'
                user_order['amount'] = order['give_remaining']
                user_order['total'] = int(order['give_remaining'] * price)
            else:
                price = calculate_price(order['get_quantity'], order['give_quantity'], supplies[order['get_asset']][1], supplies[order['give_asset']][1])
                user_order['type'] = 'BUY'
                user_order['total'] = order['give_remaining']
                user_order['amount'] = int(order['give_remaining'] / price)

            user_order['price'] = format(price, '.8f')

            if len(addresses) == 0 and len(market_orders) > 0:
                previous_order = market_orders[-1]
                if previous_order['type'] == user_order['type'] and previous_order['price'] == user_order['price']:
                    market_orders[-1]['amount'] += user_order['amount']
                    market_orders[-1]['total'] += user_order['total']
                    exclude = True

            if len(addresses) > 0:
                completed = format(((D(order['give_quantity']) - D(order['give_remaining'])) / D(order['give_quantity'])) * D(100), '.2f')
                user_order['completion'] = "{}%".format(completed)
                user_order['tx_index'] = order['tx_index']
                user_order['tx_hash'] = order['tx_hash']
                user_order['source'] = order['source']
                user_order['block_index'] = order['block_index']
                user_order['block_time'] = order['block_time']

        if not exclude:
            market_orders.append(user_order)

    return market_orders


def get_market_trades(asset1, asset2, addresses=[], limit=100, supplies=None):

    base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)
    if not supplies:
        supplies = get_assets_supply([asset1, asset2])
    market_trades = []

    sources = ''
    bindings = ['expired']
    if len(addresses) > 0:
        placeholder = ','.join(['?' for e in range(0,len(addresses))])
        sources = '''AND (tx0_address IN ({}) OR tx1_address IN ({}))'''.format(placeholder, placeholder)
        bindings += addresses + addresses

    sql = '''SELECT order_matches.*, blocks.block_time FROM order_matches INNER JOIN blocks ON order_matches.block_index=blocks.block_index
             WHERE status != ? {}
                AND forward_asset IN (?, ?)
                AND backward_asset IN (?, ?)
             ORDER BY block_index DESC'''.format(sources)

    bindings +=  [asset1, asset2, asset1, asset2]

    order_matches = util.call_jsonrpc_api('sql', {'query': sql, 'bindings': bindings})['result']

    for order_match in order_matches:

        if order_match['tx0_address'] in addresses:
            trade = {}
            trade['match_id'] = order_match['id']
            trade['source'] = order_match['tx0_address']
            trade['countersource'] = order_match['tx1_address']
            trade['block_index'] = order_match['block_index']
            trade['block_time'] = order_match['block_time']
            trade['status'] = order_match['status']
            if order_match['forward_asset'] == base_asset:
                trade['type'] = 'SELL'
                trade['price'] = format_price(order_match['forward_quantity'], order_match['backward_quantity'], supplies[order_match['forward_asset']][1], supplies[order_match['backward_asset']][1])
                trade['amount'] = order_match['forward_quantity']
                trade['total'] = order_match['backward_quantity']
            else:
                trade['type'] = 'BUY'
                trade['price'] = format_price(order_match['backward_quantity'], order_match['forward_quantity'], supplies[order_match['backward_asset']][1], supplies[order_match['forward_asset']][1])
                trade['amount'] = order_match['backward_quantity']
                trade['total'] = order_match['forward_quantity']
            market_trades.append(trade)

        if len(addresses)==0 or order_match['tx1_address'] in addresses:
            trade = {}
            trade['match_id'] = order_match['id']
            trade['source'] = order_match['tx1_address']
            trade['countersource'] = order_match['tx0_address']
            trade['block_index'] = order_match['block_index']
            trade['block_time'] = order_match['block_time']
            trade['status'] = order_match['status']
            if order_match['backward_asset'] == base_asset:
                trade['type'] = 'SELL'
                trade['price'] = format_price(order_match['backward_quantity'], order_match['forward_quantity'], supplies[order_match['backward_asset']][1], supplies[order_match['forward_asset']][1])
                trade['amount'] = order_match['backward_quantity']
                trade['total'] = order_match['forward_quantity']
            else:
                trade['type'] = 'BUY'
                trade['price'] = format_price(order_match['forward_quantity'], order_match['backward_quantity'], supplies[order_match['forward_asset']][1], supplies[order_match['backward_asset']][1])
                trade['amount'] = order_match['forward_quantity']
                trade['total'] = order_match['backward_quantity']
            market_trades.append(trade)

    return market_trades


def get_assets_supply(assets=[]):

    supplies = {}

    if 'SHP' in assets:
        supplies['SHP'] = (util.call_jsonrpc_api('get_xcp_supply', [])['result'], True)
        assets.remove('SHP')

    if 'SCH' in assets:
        supplies['SCH'] = (0, True)
        assets.remove('SCH')

    if len(assets) > 0:
        sql = '''SELECT asset, SUM(quantity) AS supply, divisible FROM issuances
                 WHERE asset IN ({})
                 AND status = ?
                 GROUP BY asset
                 ORDER BY asset'''.format(','.join(['?' for e in range(0,len(assets))]))
        bindings = assets + ['valid']

        issuances = util.call_jsonrpc_api('sql', {'query': sql, 'bindings': bindings})['result']
        for issuance in issuances:
            supplies[issuance['asset']] = (issuance['supply'], issuance['divisible'])

    return supplies


def get_pair_price(base_asset, quote_asset, max_block_time=None, supplies=None):

    if not supplies:
        supplies = get_assets_supply([base_asset, quote_asset])

    sql = '''SELECT *, MAX(tx0_index, tx1_index) AS tx_index, blocks.block_time
             FROM order_matches INNER JOIN blocks ON order_matches.block_index = blocks.block_index
             WHERE
                forward_asset IN (?, ?) AND
                backward_asset IN (?, ?) '''
    bindings = [base_asset, quote_asset, base_asset, quote_asset]

    if max_block_time:
        sql += '''AND block_time <= ? '''
        bindings += [max_block_time]

    sql += '''ORDER BY tx_index DESC
             LIMIT 2'''

    order_matches = util.call_jsonrpc_api('sql', {'query': sql, 'bindings': bindings})['result']

    if len(order_matches) == 0:
        last_price = D(0.0)
    elif order_matches[0]['forward_asset'] == base_asset:
        last_price = calculate_price(order_matches[0]['forward_quantity'], order_matches[0]['backward_quantity'], supplies[order_matches[0]['forward_asset']][1], supplies[order_matches[0]['backward_asset']][1])
    else:
        last_price = calculate_price(order_matches[0]['backward_quantity'], order_matches[0]['forward_quantity'], supplies[order_matches[0]['backward_asset']][1], supplies[order_matches[0]['forward_asset']][1])

    trend = 0
    if len(order_matches) == 2:
        if order_matches[1]['forward_asset'] == base_asset:
            before_last_price = calculate_price(order_matches[0]['forward_quantity'], order_matches[0]['backward_quantity'], supplies[order_matches[0]['forward_asset']][1], supplies[order_matches[0]['backward_asset']][1])
        else:
            before_last_price = calculate_price(order_matches[0]['backward_quantity'], order_matches[0]['forward_quantity'], supplies[order_matches[0]['backward_asset']][1], supplies[order_matches[0]['forward_asset']][1])
        if last_price < before_last_price:
            trend = -1
        elif last_price > before_last_price:
            trend = 1

    return D(last_price), trend

def get_price_movement(base_asset, quote_asset, supplies=None):

    yesterday = int(time.time() - (24*60*60))
    if not supplies:
        supplies = get_assets_supply([base_asset, quote_asset])

    price, trend = get_pair_price(base_asset, quote_asset, supplies=supplies)
    price24h, trend24h = get_pair_price(base_asset, quote_asset, max_block_time=yesterday, supplies=supplies)
    try:
        progression = (price - price24h) / (price24h / D(100))
    except:
        progression = D(0)

    return price, trend, price24h, progression

def get_markets_list(mongo_db=None):

    yesterday = int(time.time() - (24*60*60))
    markets = []
    pairs = []

    # pairs with volume last 24h
    pairs += get_xcp_and_btc_pairs(exclude_pairs=[], max_pairs=50, from_time=yesterday)
    pair_with_volume = [p['pair'] for p in pairs]

    # pairs without volume last 24h
    pairs += get_xcp_and_btc_pairs(exclude_pairs=pair_with_volume, max_pairs=50)

    base_assets  = [p['base_asset'] for p in pairs]
    quote_assets  = [p['quote_asset'] for p in pairs]
    all_assets = list(set(base_assets + quote_assets))
    supplies = get_assets_supply(all_assets)

    asset_with_image = {}
    if mongo_db:
        infos = mongo_db.asset_extended_info.find({'asset': {'$in': all_assets}}, {'_id': 0}) or False
        for info in infos:
            if 'info_data' in info and 'valid_image' in info['info_data'] and info['info_data']['valid_image']:
                asset_with_image[info['asset']] = True

    for pair in pairs:
        price, trend, price24h, progression = get_price_movement(pair['base_asset'], pair['quote_asset'], supplies=supplies)
        market = {}
        market['base_asset'] = pair['base_asset']
        market['quote_asset'] = pair['quote_asset']
        market['volume'] = pair['quote_quantity'] if pair['pair'] in pair_with_volume else 0
        market['price'] = format(price, ".8f")
        market['trend'] = trend
        market['progression'] = format(progression, ".2f")
        market['price_24h'] = format(price24h, ".8f")
        market['supply'] = supplies[pair['base_asset']][0]
        market['divisible'] = supplies[pair['base_asset']][1]
        market['market_cap'] = format(D(market['supply']) * D(market['price']), ".4f")
        market['with_image'] = True if pair['base_asset'] in asset_with_image else False
        if market['base_asset'] == 'SHP' and market['quote_asset'] == 'SCH':
            markets.insert(0, market)
        else:
            markets.append(market)

    for m in range(len(markets)):
        markets[m]['pos'] = m + 1

    return markets


def get_market_details(asset1, asset2, min_fee_provided=0.95, max_fee_required=0.95, mongo_db=None):

    yesterday = int(time.time() - (24*60*60))
    base_asset, quote_asset = util.assets_to_asset_pair(asset1, asset2)

    supplies = get_assets_supply([base_asset, quote_asset])

    price, trend, price24h, progression = get_price_movement(base_asset, quote_asset, supplies=supplies)

    buy_orders = []
    sell_orders = []
    market_orders = get_market_orders(base_asset, quote_asset, supplies=supplies, min_fee_provided=min_fee_provided, max_fee_required=max_fee_required)
    for order in market_orders:
        if order['type'] == 'SELL':
            sell_orders.append(order)
        elif order['type'] == 'BUY':
            buy_orders.append(order)

    last_trades =  get_market_trades(base_asset, quote_asset, supplies=supplies)

    ext_info = False
    if mongo_db:
        ext_info = mongo_db.asset_extended_info.find_one({'asset': base_asset}, {'_id': 0})
        if ext_info and 'info_data' in ext_info:
            ext_info = ext_info['info_data']
        else:
            ext_info = False

    return {
        'base_asset': base_asset,
        'quote_asset': quote_asset,
        'price': format(price, ".8f"),
        'trend': trend,
        'progression': format(progression, ".2f"),
        'price_24h': format(price24h, ".8f"),
        'supply': supplies[base_asset][0],
        'base_asset_divisible': supplies[base_asset][1],
        'quote_asset_divisible': supplies[quote_asset][1],
        'buy_orders': sorted(buy_orders, key=lambda x: x['price'], reverse=True),
        'sell_orders': sorted(sell_orders, key=lambda x: x['price']),
        'last_trades': last_trades,
        'base_asset_infos': ext_info
    }




