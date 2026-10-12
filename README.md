# Fraud Detection — Tables or Graphs?

A 1.1 million node fraud-detection dataset published in one format that loads
into **both** a graph database and a relational one, so the same questions can
be asked of each and the answers compared.

Built for a DBMS group project comparing graph and relational engines on
identical data.

## What's here

```
fraud_1_1m/
  csv/                       11 CSV files, 89 MB
  load_memgraph.cypher       indexes, LOAD CSV, constraints
  load_mysql.sql             schema, 7 tables, 10 foreign keys, LOAD DATA
  README.md                  full schema, mapping and verification queries
scripts/
  generate_fraud_data.py     regenerates the dataset at any scale
```

| | Count |
| --- | --- |
| nodes | 1,100,000 |
| relationships | 3,238,159 |
| transactions | 1,068,669 |
| clients (of which mules) | 8,038 (1,431) |
| merchants / banks | 1,146 / 10 |
| SSN / Email / Phone | 7,393 / 7,364 / 7,380 |

## Provenance

The data is **synthetic**. Its shape is measured from the
[neo4j-graph-examples/fraud-detection](https://github.com/neo4j-graph-examples/fraud-detection)
example graph — a PaySim simulation of 332,973 nodes — and reproduced at 3.30×
that size by `scripts/generate_fraud_data.py` (seed 42, so it is reproducible
byte for byte). No real customer data is involved.

What carries over from the source, beyond the node mix, is the part that makes
fraud queries meaningful:

- Fraud occurs only on `Transfer` (3.59%) and `Payment` (0.26%), never on
  `CashIn`, `CashOut` or `Debit`.
- **Every fraudulent Transfer goes to a Mule**; fraudulent Payments go to
  Merchants.
- Identities are shared between clients — 8,038 clients hold only 7,393 SSNs,
  one of them shared by 21 clients — so fraud rings exist to be found. 3,935
  clients sit one shared identifier away from a mule.
- Client activity is power-law skewed, ~133 transactions each with a long tail,
  so lookups and traversals vary in cost the way they do in real data.

## The model

```
(:Client)-[:PERFORMED]->(:Transaction)          one performer per transaction
(:Transaction)-[:TO]->(:Merchant|:Client|:Bank) exactly one target
(:Transaction)-[:NEXT]->(:Transaction)          one chain per client
(:Client)-[:FIRST_TX|LAST_TX]->(:Transaction)   the ends of that chain
(:Client)-[:HAS_SSN]->(:SSN)                    shared between clients
(:Client)-[:HAS_EMAIL]->(:Email)                shared between clients
(:Client)-[:HAS_PHONE]->(:Phone)                shared between clients
```

Every transaction is `:Transaction` plus one of `:CashIn` `:CashOut` `:Payment`
`:Transfer` `:Debit`; a fraudulent account is `:Client:Mule`.

The relational side expresses the same thing with foreign keys. Where the two
models genuinely differ:

| Graph | MySQL |
| --- | --- |
| the `:Mule` label | `clients.is_mule` |
| the transaction subtype label | `transactions.type` (ENUM) |
| `TO` → Merchant, Client **or** Bank | three nullable FKs, exactly one set |
| `NEXT` chain | `transactions.next_transaction_id`, self-referencing |
| shared `:SSN`/`:Email`/`:Phone` nodes | `clients.ssn` / `.email` / `.phone_number` FKs |

## Loading

**MySQL 8.x** — ~27 seconds. Paths in the loader are relative, so run it from
inside the dataset directory:

```bash
cd fraud_1_1m
mysql -u root -p -e "SET GLOBAL local_infile=1;"
mysql --local-infile=1 -u root -p < load_mysql.sql
mysql -u root -p -e "SET GLOBAL local_infile=0;"
```

**Memgraph 2.x / 3.x** — ~18 seconds, 1.58 GiB resident. The server process
reads the files, so copy them in first:

```bash
cd fraud_1_1m
docker exec -u root memgraph-mage rm -rf /csv   # docker cp nests otherwise
docker cp csv memgraph-mage:/csv
docker exec -i memgraph-mage mgconsole < load_memgraph.cypher
```

**Neo4j** — the same CSVs work via `LOAD CSV WITH HEADERS` inside
`CALL { ... } IN TRANSACTIONS OF 10000 ROWS`, or `neo4j-admin database import
full` with its own header format (`:ID`, `:LABEL`, `:START_ID`, `:END_ID`).

Transactions are split into one CSV per type because neither engine can take a
label from a CSV column; MySQL reads the `type` column from those same files.

## Regenerating, at any size

```bash
python3 scripts/generate_fraud_data.py --nodes 1100000 --out fraud_1_1m --schema fraud_1_1m
python3 scripts/generate_fraud_data.py --nodes 10000000 --out fraud_10m --schema fraud_10m
```

Only the standard library is needed. The proportions, skew and fraud patterns
hold at any `--nodes`; 1.1M takes about 3 seconds to generate.

## Checking a load

`fraud_1_1m/README.md` has the queries, each written for both engines and with
its expected answer — fraud by type and target, identity-sharing ring sizes, a
mule-to-client traversal, and a referential-integrity sweep (MySQL loads with
foreign key checks off for speed, so that last one matters).
