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

if len(sys.argv) != 2:
    raise Exception("Must pass in number of MB for transaction cache")

num_MB = int(sys.argv[1])

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

# How many times a utxo has to go from Top->Bottom to be
# have its spending tx cached(if otherwise empty)
# Increasing this value reducs false positive rates
# and reduces memory usage accordingly.
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

def estimatesmartfee(block_count):
    # Create the RPC request payload
    payload = json.dumps({
        "jsonrpc": "1.0",
        "id": "anticycle",
        "method": "estimatesmartfee",
        "params": [block_count]
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

    # txid -> serialized_tx
    # Cache for full transactions of which
    # we believe are being replacement cycled.
    cycled_tx_cache = {}
    cycled_tx_cache_size = 0

    # utxo
    # The complete set of inputs that are spent
    # by protected transactions. This ensures
    # that every cached tx in cycled_tx_cache
    # can be independently spent, costing the attacker
    # a full "top block" slot each on inclusion.
    cycled_input_set = set([])

    # txid -> serialize_tx
    # This cache is for everything above "top block"
    # that we hear about. This cache is required only
    # because the R(emove) notification stream doesn't
    # give full transactions. We need them to compute
    # top->bottom utxo changes.
    dummy_cache = {}
    dummy_cache_size = 0

    # utxo -> int
    # How many times in this epoch has the specific utxo
    # gone from next block to non-next block?
    utxo_cycled_count = defaultdict(int)

    # utxo -> txid
    # Assign txids of protected transactions to utxos that
    # appear to be replacement cycled. The full tx is
    # fetched from cycled_tx_cache.
    utxo_cache = {}

    # Simple anti-DoS max
    tx_cache_max_byte_size = num_MB * 1000 * 1000

    # These are populated by "R" events and cleared in
    # subsequent "A" events. These are to track
    # top->bottom transitions
    # utxo -> removed tx's txid
    utxos_being_doublespent = {}

    logging.info("Getting Top Block fee")

    # "Top block" is considered next three blocks
    topblock_rate_btc_kvb = estimatesmartfee(3)["feerate"]

    try:
        while True:
            # Receive a message
            topic, body, sequence = socket.recv_multipart()
            received_seq = struct.unpack('<I', sequence)[-1]
            txid = body[:32].hex()
            label = chr(body[32])

            if received_seq % 100 == 0:
                logging.info(f"Transactions cached: {len(cycled_tx_cache)}, bytes cached: {cycled_tx_cache_size/1000000}/{num_MB}MB, topblock rate: {topblock_rate_btc_kvb}")
                logging.info(f"Dummy cache: {len(dummy_cache)}, {dummy_cache_size/1000000}/{num_MB}MB")

            if label == "A":
                logging.info(f"Tx {txid} added")
                entry = getmempoolentry(txid)
                if entry is None:
                    utxos_being_doublespent.clear()
                    continue
                # We are allowing "packages" of ancestors.
                # What we really want is the mempool entry's chunk feerate.
                # And we actually don't want to track in-mempool utxos, only
                # confirmed.
                tx_rate_btc_kvb = Decimal(entry['fees']['ancestor']) / entry['ancestorsize'] * 1000
                new_top_block = tx_rate_btc_kvb >= topblock_rate_btc_kvb 
                if new_top_block:
                    raw_tx = getrawtransaction(txid)
                    # Might have already been evicted/mined/etc
                    if raw_tx is None:
                        utxos_being_doublespent.clear()
                        continue
                    tx_bytes = bytes.fromhex(raw_tx["hex"])

                    # Cache tx to make sure we see it when it's being removed later
                    # FIXME get a better notification stream
                    dummy_cache[txid] = raw_tx
                    dummy_cache_size += len(raw_tx["hex"]) / 2

                    add_tx_prevouts = [(tx_input['txid'], tx_input['vout']) for tx_input in raw_tx["vin"]]

                    for prevout in add_tx_prevouts:
                        if prevout not in utxos_being_doublespent:
                            # Bottom->Top, clear cached transaction if any
                            if prevout in utxo_cache:
                                logging.info(f"Deleting cache entry for {(tx_input['txid'], tx_input['vout'])}")
                                deleted_prevouts = [(tx_input['txid'], tx_input['vout']) for tx_input in cycled_tx_cache[utxo_cache[prevout]]["vin"]]
                                cycled_tx_cache_size -= len(cycled_tx_cache[utxo_cache[prevout]]["hex"]) / 2
                                del cycled_tx_cache[utxo_cache[prevout]]
                                del utxo_cache[prevout]
                                for deleted_prevout in deleted_prevouts:
                                    if deleted_prevout in cycled_input_set:
                                        cycled_input_set.remove(deleted_prevout)
                        else:
                            # Top->Top, cache if entry is empty
                            if prevout not in utxo_cache and utxo_cycled_count[prevout] >= CYCLE_THRESH:
                                # Get replaced txid and full tx from dummy_cache
                                removed_txid = utxos_being_doublespent[prevout]
                                removed_tx = dummy_cache[removed_txid]
                                removed_prevouts = [(tx_input['txid'], tx_input['vout']) for tx_input in raw_tx["vin"]]
                                can_cache = all(prevout not in cycled_input_set for prevout in removed_prevouts)
                                if can_cache:
                                    logging.info(f"{prevout} has been RBF'd, caching {removed_txid}")
                                    utxo_cache[prevout] = removed_txid
                                    cycled_tx_cache[removed_txid] = removed_tx
                                    cycled_tx_cache_size += len(cycled_tx_cache[utxo_cache[prevout]]["hex"]) / 2
                                    for removed_prevout in removed_prevouts:
                                        cycled_input_set.add(removed_prevout)
                                else:
                                    logging.info(f"{removed_txid} is not being cached due to conflicts in input cache")
                            del utxos_being_doublespent[prevout] # delete to detect remaining Top->Bottom later

                    # Handle Top->Bottom: There are top block spends now unspent!
                    if len(utxos_being_doublespent) > 0:
                        # things were double-spent and not removed with top block
                        for unspent_prevout, _ in utxos_being_doublespent.items():
                            # Count it first
                            utxo_cycled_count[unspent_prevout] += 1
                            logging.info(f"{unspent_prevout} has been cycled {utxo_cycled_count[unspent_prevout]} times")

                            # If we have something cached, it might be free to re-enter now
                            if unspent_prevout in utxo_cache and utxo_cache[unspent_prevout] in cycled_tx_cache:
                                raw_tx = cycled_tx_cache[utxo_cache[unspent_prevout]]["hex"]
                                send_ret = sendrawtransaction(raw_tx)
                                if send_ret:
                                    logging.info(f"Successfully resubmitted {send_ret}")
                                    logging.info(f"rawhex: {raw_tx}")

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
                if txid in dummy_cache:
                    for tx_input in dummy_cache[txid]["vin"]:
                        utxos_being_doublespent[(tx_input["txid"], tx_input["vout"])] = txid

            elif label == "C" or label == "D":
                logging.info(f"Block tip changed")
                # FIXME do something smarter, for now we just hope this isn't hit on short timeframes
                # Defender will have to resubmit enough again to be protected for the new period
                if cycled_tx_cache_size > tx_cache_max_byte_size or dummy_cache_size >= tx_cache_max_byte_size:
                    logging.info(f"wiping state")
                    dummy_cache.clear()
                    dummy_cache_size = 0
                    utxo_cache.clear()
                    utxo_cycled_count.clear()
                    utxos_being_doublespent.clear()
                    cycled_tx_cache.clear()
                    cycled_tx_cache_size = 0
                topblock_rate_btc_kvb = estimatesmartfee(3)["feerate"]
    except KeyboardInterrupt:
        logging.info("Program interrupted by user")
    finally:
        # Clean up on exit
        socket.close()
        context.term()
        
if __name__ == "__main__":
    main()

