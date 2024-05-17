# anticycle
Demo anti-cycle replacement script

https://bitcoinops.org/en/newsletters/2023/10/25/#replacement-cycling-vulnerability-against-htlcs

Requires ZMQ and RPC access to your Bitcoin Core node

Horrifically untested, won't stand up to real attacks
probably, maybe? For now, only tracks/attempts single
tx resubmissions for RBFs. In other words, it will
not attempt CPFP-based fee transaction structures,
though that can be supported in future.

Mostly implementing to see how many times this cycling
seems to be happening in practice to set the cycling
threshhold.

```mermaid
flowchart TD
    A(UTXO Spend in Top Block) -->|evicted chunk\ncached\nif empty| A
    A -->|evicted chunk\ncached\nif empty,\ntry resubmitting\ncached| B(UTXO spend NOT in Top Block)
    B -->|clear cache for utxo| A
```

TODO: Figure out more comprehensive anti-DoS story against
an attacker simply churning the cache with incremental RBFs:

1. Only cache full tx when CYCLE_THRESH breached, vs simply being added
 - utxo as anti-DoS?
 - requires some other publication mechanism?
2. Increase CYCLE_THRESH, for multiplicative security (costing attacker more per slot)
3. Increase max memory usage X times, for multiplicative security
4. Cache to disk for additional security
