# Fraud Detection — Tables or Graphs?

A 1.1 million node fraud-detection dataset published in one format that loads
into **Neo4j, Memgraph and MySQL**, so the same questions can be asked of each
and the answers compared.

Built for a DBMS group project comparing graph and relational engines on
identical data.

| | Count | | Load time |
| --- | --- | --- | --- |
| nodes | 1,100,000 | Memgraph | 18 s |
| relationships | 3,238,159 | Neo4j | 40 s |
| transactions | 1,068,669 | MySQL | 27 s |
| clients (of which mules) | 8,038 (1,431) | | |
| merchants / banks | 1,146 / 10 | CSV on disk | 89 MB |
| SSN / Email / Phone | 7,393 / 7,364 / 7,380 | repo clone | 110 MB |

All three engines return identical answers; the verification queries and their
expected results are in [`fraud_1_1m/README.md`](fraud_1_1m/README.md).

## Get the files

```bash
git clone https://github.com/Rigesh50/Fraud-Detection-Tables-or-Graphs-.git
cd Fraud-Detection-Tables-or-Graphs-/fraud_1_1m
```

Or **Code → Download ZIP** on GitHub, if you'd rather not use git.

```
fraud_1_1m/
  csv/                       11 CSV files, 89 MB — the data itself
  load_neo4j.cypher          LOAD CSV for Neo4j 5.x+ (incl. Neo4j Desktop)
  load_memgraph.cypher       LOAD CSV for Memgraph 2.x / 3.x
  load_mysql.sql             schema, 7 tables, 10 foreign keys, LOAD DATA
  README.md                  schema, graph↔relational mapping, verification
scripts/
  generate_fraud_data.py     regenerates the dataset at any scale
```

You only need the loader for the engine you're using. Nothing has to be
installed to *read* the CSVs — they're plain text.

## Neo4j Desktop

1. Create a DBMS in Desktop (or use an existing one) and note its password.
   Use an **empty** database — the script creates, it does not merge.
2. Click the `...` button on the DBMS → **Open folder** → **Import**.
3. Copy the *contents* of `csv/` into that folder — the eleven `.csv` files
   themselves, **not** the `csv/` folder. Neo4j resolves `file:///` against
   that one folder and nowhere else.
4. Start the DBMS, then run the loader. Either paste `load_neo4j.cypher` into
   Neo4j Browser (it runs statement by statement), or from the DBMS's `...` →
   **Terminal**:

```bash
bin/cypher-shell -u neo4j -p <password> -f /path/to/load_neo4j.cypher
```

Takes about 40 seconds. On a plain server install, `cp csv/*.csv
"$NEO4J_HOME/import/"` and run the same `cypher-shell -f` command.

**Or restore a ready-made database.** A Neo4j dump of this dataset is attached
to [release v1.0-neo4j-dump](https://github.com/Rigesh50/Fraud-Detection-Tables-or-Graphs-/releases/tag/v1.0-neo4j-dump) — in Desktop, **Add** → **File**, pick the
`.dump`, then its `...` → **Create new DBMS from dump**. Seconds instead of 40,
and no import folder to fiddle with.

> **Check your Neo4j version first.** A dump only restores into a server the
> **same or newer** than the one that wrote it, and that file was written by
> **Neo4j 2026.09.0**. On 5.x or 2025.x — what most Desktop installs run — it
> will not load, and no flag fixes it (`migrate` only moves a store forward).
> It is also 297 MB against 88 MB of CSV, and it is Neo4j-only. If in doubt,
> load the CSVs above.

## Memgraph

### Memgraph, the easy way — one file, no mounts, 24 seconds

`fraud_1_1m.cypherl.gz` (29 MB) carries the data inside the statements, so
Memgraph never has to read a file. Nothing to mount, nothing to `docker cp`, no
`/csv` path to get wrong — it streams in over stdin, still compressed:

```bash
cd fraud_1_1m
gunzip -c fraud_1_1m.cypherl.gz | docker exec -i mg-fraud mgconsole
```

```bash
gunzip -c fraud_1_1m.cypherl.gz | mgconsole          # native install
```

Substitute your own container name. The database must be **empty** — this
creates, it does not merge. 24 seconds, against 13-18 for the CSV routes below,
which is a fair trade for having no filesystem to argue with.

The file is 4,386 statements, batching rows with `UNWIND` rather than one
`CREATE` per row — one statement per row would be 4.3 million of them, a far
bigger file, and slow, since each would commit separately. Regenerate it with
`python3 scripts/fraud_csv_to_cypherl.py --csv fraud_1_1m/csv --out fraud_1_1m.cypherl`.

### From the CSVs instead

Memgraph's server process reads the CSVs itself, so they have to be somewhere
it can see them — inside the VM/container, if you're running it in Docker or
Colima. The `docker` commands below are identical either way; what differs is
where you set the memory.

**Give the VM at least 4 GB.** The loaded graph sits at 1.58 GiB and will not
fit in the 2 GB default of either runtime.

```bash
# Colima — memory is a VM setting, not a Docker preference
colima start --memory 4 --cpu 4          # or: colima stop && colima start --memory 4
# Docker Desktop — Settings → Resources → Memory
```

Then start Memgraph with `csv/` mounted, which avoids copying anything:

```bash
cd fraud_1_1m
docker run -d --name mg-fraud -p 7687:7687 \
  -v "$(pwd)/csv:/csv:ro" memgraph/memgraph-mage:latest

docker exec -i mg-fraud mgconsole < load_memgraph.cypher
```

13 seconds, measured on Colima (4 CPU / 8 GiB, virtiofs).

**`Bind for 0.0.0.0:7687 failed: port is already allocated`** means another
Memgraph is already running. Either load into that one with the `docker cp`
route below, or give the new container a different host port, `-p 7688:7687`,
and point clients at 7688. Publishing `7444` is not needed at all — it only
streams logs to Memgraph Lab — so it is left out above to avoid one more port
that can clash.

> **On Colima the clone must live under your home directory.** Colima only
> passes `/Users/<you>` into the VM, and a `-v` mount of any other path —
> `/tmp`, an external volume — silently appears as an *empty* directory, so the
> load finds no files and creates nothing. There is no error.

**Already have a Memgraph container running?** You can't add a mount to a live
container, so copy the files in instead:

```bash
cd fraud_1_1m
docker exec -u root memgraph-mage rm -rf /csv   # docker cp nests otherwise
docker cp csv memgraph-mage:/csv
docker exec -i memgraph-mage mgconsole < load_memgraph.cypher
```

18 seconds. Note both quirks: `docker cp csv container:/csv` creates
`/csv/csv` when `/csv` already exists, and the container's own user cannot
delete root-owned files there — hence `-u root rm -rf` first. A stale `/csv`
from an earlier load is the one failure here that produces plausible-looking
wrong data rather than an error.

**No container at all** (native install)? Point the script at the real path:

```bash
sed -i '' "s|/csv/|$(pwd)/csv/|g" load_memgraph.cypher   # macOS; drop the '' on Linux
mgconsole < load_memgraph.cypher
```

**Memgraph Lab** (the GUI, `http://localhost:3000`) is the pleasant way to
query the graph afterwards, but run the load through `mgconsole` as above — the
file is 20+ statements meant to be streamed in.

Memgraph Community holds a single graph, so this replaces whatever is already
loaded. To start clean, drop the constraints first — analytical mode refuses to
engage while they exist:

```cypher
DROP CONSTRAINT ON (n:Transaction) ASSERT n.id IS UNIQUE;  -- and Client, Bank,
DROP CONSTRAINT ON (n:SSN) ASSERT n.ssn IS UNIQUE;         -- Merchant, Email, Phone
STORAGE MODE IN_MEMORY_ANALYTICAL;
DROP GRAPH;
STORAGE MODE IN_MEMORY_TRANSACTIONAL;
```

## MySQL 8.x

The loader creates the schema, seven tables, ten foreign keys and the indexes.
Its CSV paths are relative, so run it **from inside `fraud_1_1m/`** —
`LOAD DATA LOCAL INFILE` resolves them against the client's working directory.

```bash
cd fraud_1_1m
mysql -u root -p -e "SET GLOBAL local_infile=1;"
mysql --local-infile=1 -u root -p < load_mysql.sql
mysql -u root -p -e "SET GLOBAL local_infile=0;"
```

Takes about 27 seconds. `LOAD DATA LOCAL INFILE` has to be enabled on **both**
sides — the `--local-infile=1` flag covers the client, and the `SET GLOBAL`
covers the server (it needs a privileged account, and reverting it afterwards is
good hygiene). Without it you get:

```
ERROR 3948 (42000): Loading local data is disabled;
this must be enabled on both the client and server sides
```

If you can't enable it at all, move `csv/` into the directory named by
`SELECT @@secure_file_priv;` and change `LOAD DATA LOCAL INFILE` to
`LOAD DATA INFILE` in the script.

## Troubleshooting

Every message below is verbatim from actually hitting the problem.

**MySQL — `ERROR 3948 (42000): Loading local data is disabled; this must be
enabled on both the client and server sides`**
The `--local-infile=1` flag covers the client only. Run
`SET GLOBAL local_infile=1;` as a privileged user, load, then set it back to 0.
If you have no such account, move `csv/` into the folder named by
`SELECT @@secure_file_priv;` and change `LOAD DATA LOCAL INFILE` to
`LOAD DATA INFILE`.

**MySQL — `ERROR 2 (HY000) at line 1: File 'csv/banks.csv' not found (OS
errno 2 - No such file or directory)`**
The loader's paths are relative. Run it from inside `fraud_1_1m/`, not from the
repo root.

**Memgraph — `Client received query exception: CSV file not found: /csv/banks.csv`**
The server cannot see the files. In a container they must be mounted at `/csv`
or copied there with `docker cp`; a native install needs the paths rewritten
(see the Memgraph section). Remember it is the *server's* filesystem that
matters, not yours.

**Memgraph — the load runs with no errors but `MATCH (n) RETURN count(n)`
returns 0 or a number from an earlier load**
Two causes, neither of which announces itself:
- On Colima, a `-v` mount of a path outside your home directory appears as an
  empty directory. Move the clone under `/Users/<you>`.
- `docker cp csv container:/csv` nests into `/csv/csv` when `/csv` already
  exists, leaving the *old* files in place. Run
  `docker exec -u root <container> rm -rf /csv` first.

**Memgraph — `rm: cannot remove '/csv/banks.csv': Permission denied`**
`docker cp` writes as root; the container's own user cannot delete those files.
Add `-u root` to the `docker exec`.

**Memgraph — `Constraints are not supported in analytical storage mode. Please
drop them before changing storage mode to analytical or use transactional
mode.`**
A previous load left its uniqueness constraints behind. Drop the seven listed
in the Memgraph section, then switch mode and `DROP GRAPH`.

**Memgraph — the container dies or the load aborts partway**
Out of memory. The graph needs 1.58 GiB and neither Colima nor Docker Desktop
gives 2 GB-plus by default: `colima start --memory 4 --cpu 4`, or Docker
Desktop → Settings → Resources.

**Neo4j — `22N43: data exception - unable to load external resource. Could not
load external resource from file:///banks.csv.`**
The CSVs are not in the database's import folder, or the `csv/` folder itself
was copied in rather than its contents. `file:///banks.csv` resolves to
`<import folder>/banks.csv`, so the eleven files must sit there loose. In
Desktop: the DBMS's `...` → **Open folder** → **Import**.

**Neo4j — `22N79: property uniqueness constraint violated. ... already exists
with label 'Bank' and property 'id' = ...`** (Neo4j 5.x words the same thing as
`Neo.ClientError.Schema.ConstraintValidationFailed`)
The database wasn't empty. The loader creates rather than merges, so run it
against a fresh one.

**Neo4j — a `.dump` you made elsewhere won't load: `not on a supported version
(current format: ...). Use the 'neo4j-admin database migrate' command`**
Dumps move forward only. Run
`neo4j-admin database migrate <db> --force-btree-indexes-to-range` after
loading an *older* dump. A *newer* dump cannot be restored into an older Neo4j
at all — including the one attached to this repo's release, which needs Neo4j
2026.09+. Use the CSVs on anything older.

**Any engine — counts don't match 1,100,000**
Compare against the per-label and per-table numbers in
[`fraud_1_1m/README.md`](fraud_1_1m/README.md); it also has the
referential-integrity sweep for MySQL, which loads with foreign key checks off
and so will happily accept a partial load.

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

Transactions are split into one CSV per type because **no engine here can take
a label from a CSV column** — that's why `csv/` has five transaction files
rather than one, and MySQL reads the `type` column from those same files.

## Regenerating, at any size

```bash
python3 scripts/generate_fraud_data.py --nodes 1100000  --out fraud_1_1m --schema fraud_1_1m
python3 scripts/generate_fraud_data.py --nodes 10000000 --out fraud_10m  --schema fraud_10m
```

Standard library only — no pip install. The proportions, skew and fraud
patterns hold at any `--nodes`; 1.1M takes about 3 seconds to generate and
writes all three loaders alongside the CSVs.

## Checking a load

[`fraud_1_1m/README.md`](fraud_1_1m/README.md) has the queries, each written
for both the graph and the relational model, with expected answers — fraud by
type and target, identity-sharing ring sizes, a mule-to-client traversal, and a
referential-integrity sweep (MySQL loads with foreign key checks off for speed,
so that last one matters).

Quick sanity check, which should give 1,100,000 everywhere:

```cypher
MATCH (n) RETURN count(n);
```

```sql
SELECT (SELECT COUNT(*) FROM transactions) + (SELECT COUNT(*) FROM clients)
     + (SELECT COUNT(*) FROM merchants)    + (SELECT COUNT(*) FROM banks)
     + (SELECT COUNT(*) FROM ssns)         + (SELECT COUNT(*) FROM emails)
     + (SELECT COUNT(*) FROM phones) AS total;
```
