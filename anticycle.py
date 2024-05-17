from decimal import Decimal
from collections import defaultdict
import json
import logging
import os
import requests
from requests.auth import HTTPBasicAuth
import struct
import sys
import zmq

rpc_user = os.environ.get('RPCUSER')
rpc_password = os.environ.get('RPCPASS')
rpc_host = '127.0.0.1'
rpc_port = 8332

if not rpc_user:
    raise Exception("Must set RPCUSER env variable to connect to Bitcoin Core RPC")

if not rpc_password:
    raise Exception("Must set RPCPASS env variable to connect to Bitcoin Core RPC")

# Configure logging settings
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Replace with cluster mempool threshholds
fee_url = 'https://mempool.space/api/v1/fees/recommended'

CYCLE_THRESH = 1

def getrawtransaction(txid):
    # Create the RPC request payload
    payload = json.dumps({
        "jsonrpc": "1.0",
        "id": "anticycle",
        "method": "getrawtransaction",
        "params": [txid, 1]
    })

    # Set the headers for the request
    headers = {
        'Content-Type': 'application/json'
    }

    # Create the URL for the RPC endpoint
    url = f'http://{rpc_host}:{rpc_port}'

    # Send the RPC request
    response = requests.post(url, headers=headers, data=payload, auth=HTTPBasicAuth(rpc_user, rpc_password))

    # Check if the request was successful
    if response.status_code == 200:
        # Parse the JSON response
        result = response.json()
        return result["result"]
    else:
        logging.info(f'Error: {response.status_code}')
        logging.info(response.text)
        return None


def getmempoolentry(txid):
    # Create the RPC request payload
    payload = json.dumps({
        "jsonrpc": "1.0",
        "id": "anticycle",
        "method": "getmempoolentry",
        "params": [txid]
    })

    # Set the headers for the request
    headers = {
        'Content-Type': 'application/json'
    }

    # Create the URL for the RPC endpoint
    url = f'http://{rpc_host}:{rpc_port}'

    # Send the RPC request
    response = requests.post(url, headers=headers, data=payload, auth=HTTPBasicAuth(rpc_user, rpc_password))

    # Check if the request was successful
    if response.status_code == 200:
        # Parse the JSON response
        result = response.json()
        return result["result"]
    else:
        logging.info(f'Error: {response.status_code}')
        logging.info(response.text)
        return None

def sendrawtransaction(txid):
    # Create the RPC request payload
    payload = json.dumps({
        "jsonrpc": "1.0",
        "id": "anticycle",
        "method": "sendrawtransaction",
        "params": [txid]
    })

    # Set the headers for the request
    headers = {
        'Content-Type': 'application/json'
    }

    # Create the URL for the RPC endpoint
    url = f'http://{rpc_host}:{rpc_port}'

    # Send the RPC request
    response = requests.post(url, headers=headers, data=payload, auth=HTTPBasicAuth(rpc_user, rpc_password))

    # Check if the request was successful
    if response.status_code == 200:
        # Parse the JSON response
        result = response.json()
        return result["result"]
    else:
        # FIXME: return error, delete tx
        # if inputs missing
        logging.info(f'Error: {response.status_code}')
        logging.info(response.text)
        return None


def main():
    '''
    Best effort mempool syncing to detect replacement cycling attacks
    '''

    logging.info("Starting anticycle")

    context = zmq.Context()
    
    # Create a socket of type SUBSCRIBE
    socket = context.socket(zmq.SUB)
    
    # Connect to the publisher's socket
    port = "28332"  # specify the port you want to listen on
    socket.connect(f"tcp://localhost:{port}")
    
    # Subscribe to all messages
    # You can specify a prefix filter here to receive specific messages
    socket.setsockopt_string(zmq.SUBSCRIBE, '')
    
    logging.info(f"Listening for messages on port {port}...")

    # txid -> tx cache (FIXME do better than this)
    # We store these anytime above top block
    # when real implementation would have access
    # to these when being evicted from the mempool
    tx_cache = {}

    # utxo -> tx_spending_utxo cache (FIXME don't store spending tx N times)
    utxo_cache = {}

    # utxo -> count of topblock->nontopblock transitions
    utxo_unspent_count = defaultdict(int)

    # These are populated by "R" events and cleared in
    # subsequent "A" events. These are to track
    # top->nontop transitions
    # utxo -> replaced tx's txid
    utxos_being_doublespent = {}

    logging.info("Getting Top Block fee")
    topblock_rate_sat_vb = requests.get(fee_url).json()["fastestFee"]
    topblock_rate_btc_kvb = Decimal(topblock_rate_sat_vb) * 1000 / 100000000

    try:
        while True:
            # Receive a message
            topic, body, sequence = socket.recv_multipart()
            received_seq = struct.unpack('<I', sequence)[-1]
            txid = body[:32].hex()
            label = chr(body[32])

            if label == "A":
                logging.info(f"Tx {txid} added")
                entry = getmempoolentry(txid)
                if entry is not None:
                    if entry['ancestorcount'] != 1:
                        # Only supporting singletons for now ala HTLC-X transactions
                        # Can extend to 1P1C pretty easily.
                        continue
                    tx_rate_btc_kvb = Decimal(entry['fees']['ancestor']) / entry['ancestorsize'] * 1000
                    new_top_block = tx_rate_btc_kvb >= topblock_rate_btc_kvb 
                    if new_top_block:
                        raw_tx = getrawtransaction(txid)
                        # We need to cache if it's replaced later, since by the time
                        # we are told it's replaced, it's already gone. Would be nice
                        # to get it when it's replaced, or persist to disk, or whatever.
                        tx_cache[txid] = raw_tx

                        for tx_input in raw_tx["vin"]:
                            prevout = (tx_input['txid'], tx_input['vout'])
                            if prevout not in utxos_being_doublespent and prevout in utxo_cache:
                                # Bottom->Top, clear cached transaction
                                logging.info(f"Deleting cache entry for {(tx_input['txid'], tx_input['vout'])}")
                                del utxo_cache[prevout]
                            elif prevout in utxos_being_doublespent and prevout not in utxo_cache:
                                if utxo_unspent_count[prevout] >= CYCLE_THRESH:
                                    logging.info(f"{prevout} has been RBF'd, caching {replaced_txid}")
                                    # Top->Top, cache the replaced transaction
                                    utxo_cache[prevout] = tx_cache[utxos_being_doublespent[prevout]]
                                    del utxos_being_doublespent[prevout] # delete to detect Top->Bottom later

                    # Handle Top->Bottom: top utxos gone unspent
                    if len(utxos_being_doublespent) > 0:
                        # things were double-spent and not replaced with top block
                        for prevout, replaced_txid in utxos_being_doublespent.items():
                            if replaced_txid in tx_cache:
                                utxo_unspent_count[prevout] += 1

                                if utxo_unspent_count[prevout] >= CYCLE_THRESH:
                                    logging.info(f"{prevout} has been cycled {utxo_unspent_count[prevout]} times, maybe caching {replaced_txid}")
                                    # cache replaced tx if nothing cached for this utxo
                                    if prevout not in utxo_cache:
                                        logging.info(f"cached {replaced_txid}!")
                                        utxo_cache[prevout] = tx_cache[replaced_txid]

                                # resubmit cached utxo tx
                                send_ret = sendrawtransaction(utxo_cache[prevout]["hex"])
                                if send_ret:
                                    logging.info(f"Successfully resubmitted {send_ret}")
                                    logging.info(f"rawhex: {utxo_cache[prevout]['hex']}")

                # We processed the double-spends, clear
                utxos_being_doublespent.clear()
            elif label == "R":
                logging.info(f"Tx {txid} removed")
                # This tx is removed, perhaps replaced, next "A" message should be the tx replacing it(conflict_tx)

                # If this tx is in the tx_cache, that implies it was top block
                # we need to see which utxos being non-top block once we see
                # the next "A"
                # N.B. I am not sure at all the next "A" is actually a double-spend, that should be checked!
                # I'm going off of functional tests.
                if txid in tx_cache:
                    for tx_input in tx_cache[txid]["vin"]:
                        utxos_being_doublespent[(tx_input["txid"], tx_input["vout"])] = txid

            elif label == "C" or label == "D":
                logging.info(f"Block tip changed")
                # FIXME do something smarter, for now we just hope this isn't hit on short timeframes
                if len(tx_cache) > 10000:
                    logging.info(f"wiping state")
                    utxo_cache.clear()
                    utxo_unspent_count.clear()
                    utxos_being_doublespent.clear()
                    tx_cache.clear()
                topblock_rate_sat_vb = requests.get(fee_url).json()["fastestFee"]
                topblock_rate_btc_kvb = Decimal(topblock_rate_sat_vb) * 1000 / 100000000
    except KeyboardInterrupt:
        logging.info("Program interrupted by user")
    finally:
        # Clean up on exit
        socket.close()
        context.term()
        
if __name__ == "__main__":
    main()

