#!/usr/bin/env python3
"""Generate a synthetic fraud-detection dataset at arbitrary scale.

The shape is taken from the real `frauddetection` graph (neo4j-graph-examples,
a PaySim simulation), measured at 332,973 nodes / 980,098 relationships, and
reproduced at whatever --nodes asks for:

    node mix          CashIn 46.1% / CashOut 23.5% / Payment 23.1% /
                      Transfer 6.0% / Debit 1.4% of transactions, plus
                      clients, merchants, banks and shared identity nodes
    clients           ~133 transactions each, power-law skewed (max ~1900)
    mules             17.8% of clients
    fraud             only on Transfer (3.59%) and Payment (0.26%); every
                      fraudulent Transfer goes to a Mule, fraudulent Payments
                      go to Merchants, and CashIn/CashOut/Debit are never fraud
    identities        SSN / Email / Phone are shared between clients -- fewer
                      identity nodes than clients, up to ~8 clients deep, which
                      is the signal fraud-ring queries look for
    NEXT              each client's transactions form one chain in globalStep
                      order, with FIRST_TX / LAST_TX pointing at its ends

Writes CSVs that load into both a graph database and MySQL, plus a loader for
each. Transactions are split into one file per type because neither engine can
take a label from a CSV column, and MySQL reads the `type` column from the same
files.

    python3 scripts/generate_fraud_data.py --nodes 1100000 --out fraud_1_1m
"""

import argparse
import csv
import random
from array import array
from pathlib import Path

# Node mix of the real dataset (332,973 nodes).
REAL = {
    "CashIn": 149037, "CashOut": 76023, "Payment": 74577,
    "Transfer": 19460, "Debit": 4392,
    "clients": 2433, "ssns": 2238, "phones": 2234, "emails": 2229,
    "merchants": 347, "banks": 3,
}
TX_TYPES = ["CashIn", "CashOut", "Payment", "Transfer", "Debit"]

MULE_SHARE = 433 / 2433
FRAUD_RATE = {"Transfer": 698 / 19460, "Payment": 192 / 74577}
# Non-fraud transfers that still go to a mule: 2363 total to mules - 698 fraud.
TRANSFER_TO_MULE = (2363 - 698) / (19460 - 698)
REAL_STEPS = 718

# Mean amount per type, from the real data. Amounts are lognormal-ish.
MEAN_AMOUNT = {
    "CashIn": 139493.10, "CashOut": 126502.68, "Payment": 8756.20,
    "Transfer": 846721.70, "Debit": 5874.69,
}

FIRST_NAMES = """James Mary Robert Patricia John Jennifer Michael Linda David
Elizabeth William Barbara Richard Susan Joseph Jessica Thomas Sarah Charles
Karen Christopher Nancy Daniel Lisa Matthew Betty Anthony Margaret Mark Sandra
Donald Ashley Steven Kimberly Paul Emily Andrew Donna Joshua Michelle Bentley
Dominic Carolyn Felix Marcus Priya Omar Yuki Ingrid Hassan Mei Olga""".split()
LAST_NAMES = """Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez
Martinez Hernandez Lopez Gonzalez Wilson Anderson Thomas Taylor Moore Jackson
Martin Lee Perez Thompson White Harris Sanchez Clark Ramirez Lewis Robinson
Walker Young Allen King Wright Scott Torres Nguyen Hill Flores Peck Boyer Mccoy
Mckinney Burt Rollins Okafor Novak Fischer Haddad Sato""".split()
MERCHANT_WORDS = """Mrsa Felics Quantum Vertex Lumen Sable Orion Pinnacle Cobalt
Harbor Summit Vector Nimbus Cedar Aster Flint Onyx Prism Delta Ember Tundra
Zephyr Garnet Basalt Cypress""".split()
EMAIL_DOMAINS = ["yahoo.com", "mail.com", "gmail.com", "hotmail.com", "outlook.com"]


def skewed(rnd, n, power=3.0):
    """Power-law index into [0, n): low indices are far more likely."""
    return int(n * (rnd.random() ** power))


def ssn_of(i):
    s = f"{i:09d}"
    return f"{s[0:3]}-{s[3:5]}-{s[5:9]}"


def phone_of(i):
    s = f"{2000000000 + i:010d}"
    return f"{s[0:3]}-{s[3:6]}-{s[6:10]}"


def merchant_id_of(i):
    s = f"{10000000 + i:09d}"
    return f"{s[0:2]}-{s[2:9]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=1_100_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", required=True)
    parser.add_argument("--schema", default="fraud_1_1m", help="MySQL schema name")
    args = parser.parse_args()

    rnd = random.Random(args.seed)
    out = Path(args.out).resolve()
    csv_dir = out / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    # --- counts: keep the real proportions, land exactly on --nodes ---
    real_total = sum(REAL.values())
    n = {k: max(1, round(args.nodes * v / real_total)) for k, v in REAL.items()}
    n["CashIn"] += args.nodes - sum(n.values())  # absorb rounding
    n_tx = sum(n[t] for t in TX_TYPES)
    steps = max(1, round(REAL_STEPS * args.nodes / real_total))

    print(f"total nodes: {sum(n.values())}")
    for key in ("clients", "merchants", "banks", "ssns", "emails", "phones"):
        print(f"  {key:<10} {n[key]:>9}")
    for t in TX_TYPES:
        print(f"  {t:<10} {n[t]:>9}")
    print(f"  {'(transactions)':<10} {n_tx:>9} over {steps} steps")

    n_mules = round(n["clients"] * MULE_SHARE)
    # Mules are the first n_mules clients; transfers pick targets from them.
    is_mule = bytearray(n["clients"])
    for i in range(n_mules):
        is_mule[i] = 1

    # --- transactions: type, owner, target, amount, fraud, then NEXT chains ---
    tx_type = bytearray(n_tx)
    cursor = 0
    for index, t in enumerate(TX_TYPES):
        for _ in range(n[t]):
            tx_type[cursor] = index
            cursor += 1
    rnd.shuffle(tx_type)  # interleave types along the timeline

    tx_client = array("i", bytes(4 * n_tx))
    tx_target = array("i", bytes(4 * n_tx))
    tx_fraud = bytearray(n_tx)
    tx_next = array("i", bytes(4 * n_tx))  # 0 = end of chain, else index + 1
    first_tx = array("i", bytes(4 * n["clients"]))  # 0 = none, else index + 1
    last_tx = array("i", bytes(4 * n["clients"]))

    for i in range(n_tx):
        kind = TX_TYPES[tx_type[i]]
        client = skewed(rnd, n["clients"])  # power law: a few very busy clients
        tx_client[i] = client

        fraud = kind in FRAUD_RATE and rnd.random() < FRAUD_RATE[kind]
        tx_fraud[i] = 1 if fraud else 0

        if kind == "Transfer":
            if fraud or rnd.random() < TRANSFER_TO_MULE:
                target = skewed(rnd, n_mules, 2.0)  # mules are clients 0..n_mules
            else:
                target = n_mules + skewed(rnd, n["clients"] - n_mules)
        elif kind == "Debit":
            target = skewed(rnd, n["banks"], 1.5)
        else:
            target = skewed(rnd, n["merchants"], 2.0)
        tx_target[i] = target

        if last_tx[client]:
            tx_next[last_tx[client] - 1] = i + 1
        else:
            first_tx[client] = i + 1
        last_tx[client] = i + 1

    # --- write the reference tables ---
    def write(name, header, rows):
        path = csv_dir / name
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        print(f"  {name:<28} {path.stat().st_size / 1048576:>7.1f} MiB")

    write("banks.csv", ["id", "name"], (
        [f"B{merchant_id_of(i)}", f"Bank of {LAST_NAMES[i % len(LAST_NAMES)]}"]
        for i in range(n["banks"])
    ))
    write("merchants.csv", ["id", "name"], (
        [merchant_id_of(i), f"M{MERCHANT_WORDS[i % len(MERCHANT_WORDS)]}"
         + (" Ltd" if i % 3 == 0 else "")]
        for i in range(n["merchants"])
    ))
    write("ssns.csv", ["ssn"], ([ssn_of(i)] for i in range(n["ssns"])))
    write("phones.csv", ["phone_number"], ([phone_of(i)] for i in range(n["phones"])))
    write("emails.csv", ["email"], (
        [f"{LAST_NAMES[i % len(LAST_NAMES)].lower()}{i % 100:02d}_{i}"
         f"@{EMAIL_DOMAINS[i % len(EMAIL_DOMAINS)]}"]
        for i in range(n["emails"])
    ))

    def client_rows():
        for i in range(n["clients"]):
            name = (f"{FIRST_NAMES[skewed(rnd, len(FIRST_NAMES), 1.2)]} "
                    f"{LAST_NAMES[skewed(rnd, len(LAST_NAMES), 1.2)]}")
            yield [
                f"{4000000000000000 + i:016d}",
                name,
                is_mule[i],
                # Shared identities: skewed draws mean some are reused by
                # several clients, which is what fraud-ring queries look for.
                ssn_of(skewed(rnd, n["ssns"], 1.4)),
                f"{LAST_NAMES[(e := skewed(rnd, n['emails'], 1.4)) % len(LAST_NAMES)].lower()}"
                f"{e % 100:02d}_{e}@{EMAIL_DOMAINS[e % len(EMAIL_DOMAINS)]}",
                phone_of(skewed(rnd, n["phones"], 1.4)),
                f"tx-{first_tx[i]}" if first_tx[i] else "",
                f"tx-{last_tx[i]}" if last_tx[i] else "",
            ]

    write(
        "clients.csv",
        ["id", "name", "is_mule", "ssn", "email", "phone_number",
         "first_tx_id", "last_tx_id"],
        client_rows(),
    )

    # --- write one transactions file per type ---
    per_step = n_tx / steps
    for index, kind in enumerate(TX_TYPES):
        def rows(index=index, kind=kind):
            for i in range(n_tx):
                if tx_type[i] != index:
                    continue
                step = min(steps, int(i / per_step) + 1)
                target = tx_target[i]
                yield [
                    f"tx-{i + 1}",
                    kind,
                    round(MEAN_AMOUNT[kind] * rnd.lognormvariate(-0.35, 0.85), 2),
                    tx_fraud[i],
                    step,
                    i + 1,
                    step,
                    f"{4000000000000000 + tx_client[i]:016d}",
                    merchant_id_of(target) if kind in ("CashIn", "CashOut", "Payment") else "",
                    f"{4000000000000000 + target:016d}" if kind == "Transfer" else "",
                    f"B{merchant_id_of(target)}" if kind == "Debit" else "",
                    f"tx-{tx_next[i]}" if tx_next[i] else "",
                ]

        write(
            f"transactions_{kind.lower()}.csv",
            ["id", "type", "amount", "fraud", "step", "global_step", "ts",
             "client_id", "to_merchant_id", "to_client_id", "to_bank_id",
             "next_transaction_id"],
            rows(),
        )

    (out / "load_mysql.sql").write_text(mysql_loader(args.schema, "csv"))
    (out / "load_memgraph.cypher").write_text(memgraph_loader())
    (out / "load_neo4j.cypher").write_text(neo4j_loader())
    print(f"\nloaders: {out / 'load_mysql.sql'}"
          f"\n         {out / 'load_memgraph.cypher'}"
          f"\n         {out / 'load_neo4j.cypher'}")


def mysql_loader(schema, csv_dir):
    simple = {
        "banks": "(`id`, `name`)",
        "merchants": "(`id`, `name`)",
        "ssns": "(`ssn`)",
        "emails": "(`email`)",
        "phones": "(`phone_number`)",
        "clients": (
            "(`id`, `name`, `is_mule`, `ssn`, `email`, `phone_number`,\n"
            " @first_tx_id, @last_tx_id)\n"
            "  SET `first_tx_id` = NULLIF(@first_tx_id, ''),\n"
            "      `last_tx_id`  = NULLIF(@last_tx_id, '')"
        ),
    }
    loads = [
        f"LOAD DATA LOCAL INFILE '{csv_dir}/{table}.csv'\n"
        f"  INTO TABLE `{table}`\n"
        "  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'\n"
        "  LINES TERMINATED BY '\\n' IGNORE 1 LINES\n"
        f"  {columns};\n"
        for table, columns in simple.items()
    ]
    for kind in TX_TYPES:
        loads.append(
            f"LOAD DATA LOCAL INFILE '{csv_dir}/transactions_{kind.lower()}.csv'\n"
            "  INTO TABLE `transactions`\n"
            "  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"'\n"
            "  LINES TERMINATED BY '\\n' IGNORE 1 LINES\n"
            "  (`id`, `type`, `amount`, `fraud`, `step`, `global_step`, `ts`,\n"
            "   `client_id`, @to_merchant_id, @to_client_id, @to_bank_id,\n"
            "   @next_transaction_id)\n"
            "  SET `to_merchant_id`      = NULLIF(@to_merchant_id, ''),\n"
            "      `to_client_id`        = NULLIF(@to_client_id, ''),\n"
            "      `to_bank_id`          = NULLIF(@to_bank_id, ''),\n"
            "      `next_transaction_id` = NULLIF(@next_transaction_id, '');\n"
        )
    return f"""\
-- Synthetic fraud-detection dataset -> MySQL.
-- Generated by scripts/generate_fraud_data.py. The CSV paths are relative to
-- this file's directory, so run mysql from there (LOAD DATA LOCAL INFILE
-- resolves them against the client's working directory).
--
-- Load with (the client flag is required for LOAD DATA LOCAL INFILE):
--   cd <this directory> && mysql --local-infile=1 -u root -p < load_mysql.sql
-- If the server refuses it: SET GLOBAL local_infile = 1;
--
-- Graph -> relational mapping:
--   (:Client)-[:PERFORMED]->(:Transaction)   transactions.client_id
--   (:Transaction)-[:TO]->(:Merchant|:Client|:Bank)
--                                            transactions.to_merchant_id /
--                                            to_client_id / to_bank_id
--   (:Transaction)-[:NEXT]->(:Transaction)   transactions.next_transaction_id
--   (:Client)-[:HAS_SSN|HAS_EMAIL|HAS_PHONE]->(...)
--                                            clients.ssn / email / phone_number
--   (:Client)-[:FIRST_TX|LAST_TX]->(:Transaction)
--                                            clients.first_tx_id / last_tx_id
--   the :Mule label                          clients.is_mule
--   the :CashIn/:CashOut/... labels          transactions.type

SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS;
SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS;
SET FOREIGN_KEY_CHECKS=0;
SET UNIQUE_CHECKS=0;

CREATE DATABASE IF NOT EXISTS `{schema}`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `{schema}`;

DROP TABLE IF EXISTS `transactions`;
DROP TABLE IF EXISTS `clients`;
DROP TABLE IF EXISTS `merchants`;
DROP TABLE IF EXISTS `banks`;
DROP TABLE IF EXISTS `ssns`;
DROP TABLE IF EXISTS `emails`;
DROP TABLE IF EXISTS `phones`;

CREATE TABLE `banks` (
  `id` VARCHAR(32) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `merchants` (
  `id` VARCHAR(32) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `ssns` (
  `ssn` VARCHAR(16) NOT NULL,
  PRIMARY KEY (`ssn`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `emails` (
  `email` VARCHAR(255) NOT NULL,
  PRIMARY KEY (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `phones` (
  `phone_number` VARCHAR(32) NOT NULL,
  PRIMARY KEY (`phone_number`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `clients` (
  `id` VARCHAR(32) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `is_mule` BOOLEAN NOT NULL DEFAULT 0 COMMENT 'the :Mule label',
  `ssn` VARCHAR(16) NOT NULL,
  `email` VARCHAR(255) NOT NULL,
  `phone_number` VARCHAR(32) NOT NULL,
  `first_tx_id` VARCHAR(32) DEFAULT NULL,
  `last_tx_id` VARCHAR(32) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_clients_ssn` (`ssn`),
  KEY `idx_clients_email` (`email`),
  KEY `idx_clients_phone` (`phone_number`),
  KEY `idx_clients_mule` (`is_mule`),
  CONSTRAINT `fk_clients_ssn` FOREIGN KEY (`ssn`) REFERENCES `ssns` (`ssn`),
  CONSTRAINT `fk_clients_email` FOREIGN KEY (`email`) REFERENCES `emails` (`email`),
  CONSTRAINT `fk_clients_phone` FOREIGN KEY (`phone_number`) REFERENCES `phones` (`phone_number`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `transactions` (
  `id` VARCHAR(32) NOT NULL,
  `type` ENUM('CashIn','CashOut','Payment','Transfer','Debit') NOT NULL,
  `amount` DECIMAL(16,2) NOT NULL,
  `fraud` BOOLEAN NOT NULL DEFAULT 0,
  `step` INT UNSIGNED NOT NULL,
  `global_step` INT UNSIGNED NOT NULL,
  `ts` BIGINT UNSIGNED NOT NULL,
  `client_id` VARCHAR(32) NOT NULL COMMENT 'PERFORMED',
  `to_merchant_id` VARCHAR(32) DEFAULT NULL,
  `to_client_id` VARCHAR(32) DEFAULT NULL,
  `to_bank_id` VARCHAR(32) DEFAULT NULL,
  `next_transaction_id` VARCHAR(32) DEFAULT NULL COMMENT 'NEXT',
  PRIMARY KEY (`id`),
  KEY `idx_tx_client` (`client_id`),
  KEY `idx_tx_type` (`type`),
  KEY `idx_tx_fraud` (`fraud`),
  KEY `idx_tx_global_step` (`global_step`),
  KEY `idx_tx_merchant` (`to_merchant_id`),
  KEY `idx_tx_to_client` (`to_client_id`),
  KEY `idx_tx_bank` (`to_bank_id`),
  KEY `idx_tx_next` (`next_transaction_id`),
  CONSTRAINT `fk_tx_client` FOREIGN KEY (`client_id`) REFERENCES `clients` (`id`),
  CONSTRAINT `fk_tx_merchant` FOREIGN KEY (`to_merchant_id`) REFERENCES `merchants` (`id`),
  CONSTRAINT `fk_tx_to_client` FOREIGN KEY (`to_client_id`) REFERENCES `clients` (`id`),
  CONSTRAINT `fk_tx_bank` FOREIGN KEY (`to_bank_id`) REFERENCES `banks` (`id`),
  CONSTRAINT `fk_tx_next` FOREIGN KEY (`next_transaction_id`) REFERENCES `transactions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- clients.first_tx_id / last_tx_id are added after `transactions` exists.
ALTER TABLE `clients`
  ADD CONSTRAINT `fk_clients_first_tx` FOREIGN KEY (`first_tx_id`) REFERENCES `transactions` (`id`),
  ADD CONSTRAINT `fk_clients_last_tx` FOREIGN KEY (`last_tx_id`) REFERENCES `transactions` (`id`);

{"".join(loads)}
SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;
SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS;
"""


def memgraph_loader():
    props = """{{
    id: row.id,
    amount: ToFloat(row.amount),
    fraud: row.fraud = "1",
    step: ToInteger(row.step),
    globalStep: ToInteger(row.global_step),
    ts: ToInteger(row.ts) }}"""

    blocks = []
    for kind in TX_TYPES:
        if kind == "Transfer":
            target = 'MATCH (dst:Client {id: row.to_client_id})'
        elif kind == "Debit":
            target = 'MATCH (dst:Bank {id: row.to_bank_id})'
        else:
            target = 'MATCH (dst:Merchant {id: row.to_merchant_id})'
        blocks.append(
            f'LOAD CSV FROM "/csv/transactions_{kind.lower()}.csv" WITH HEADER AS row\n'
            f'MATCH (c:Client {{id: row.client_id}})\n'
            f'{target}\n'
            f'CREATE (c)-[:PERFORMED]->(t:Transaction:{kind} {props.format()}),\n'
            f'  (t)-[:TO]->(dst);\n'
        )
    next_blocks = [
        f'LOAD CSV FROM "/csv/transactions_{kind.lower()}.csv" WITH HEADER AS row\n'
        'WITH row WHERE row.next_transaction_id <> ""\n'
        'MATCH (a:Transaction {id: row.id}), (b:Transaction {id: row.next_transaction_id})\n'
        'CREATE (a)-[:NEXT]->(b);\n'
        for kind in TX_TYPES
    ]
    return f"""\
// Synthetic fraud-detection dataset -> Memgraph.
// Generated by scripts/generate_fraud_data.py.
//
// The CSVs must be readable by the server process. With Docker:
//   docker exec -u root memgraph-mage rm -rf /csv   // docker cp nests otherwise
//   docker cp csv memgraph-mage:/csv
//   docker exec -i memgraph-mage mgconsole < load_memgraph.cypher
//
// Indexes come first (every relationship below is a key lookup), the bulk load
// runs in analytical mode, and the uniqueness constraints go last because
// analytical mode rejects them.

STORAGE MODE IN_MEMORY_ANALYTICAL;

CREATE INDEX ON :Bank(id);
CREATE INDEX ON :Merchant(id);
CREATE INDEX ON :SSN(ssn);
CREATE INDEX ON :Email(email);
CREATE INDEX ON :Phone(phoneNumber);
CREATE INDEX ON :Client(id);
CREATE INDEX ON :Transaction(id);
CREATE INDEX ON :Transaction(globalStep);

LOAD CSV FROM "/csv/banks.csv" WITH HEADER AS row
CREATE (:Bank {{id: row.id, name: row.name}});

LOAD CSV FROM "/csv/merchants.csv" WITH HEADER AS row
CREATE (:Merchant {{id: row.id, name: row.name}});

LOAD CSV FROM "/csv/ssns.csv" WITH HEADER AS row
CREATE (:SSN {{ssn: row.ssn}});

LOAD CSV FROM "/csv/emails.csv" WITH HEADER AS row
CREATE (:Email {{email: row.email}});

LOAD CSV FROM "/csv/phones.csv" WITH HEADER AS row
CREATE (:Phone {{phoneNumber: row.phone_number}});

// Clients load in two passes because a label cannot come from a CSV column.
LOAD CSV FROM "/csv/clients.csv" WITH HEADER AS row
WITH row WHERE row.is_mule = "0"
MATCH (s:SSN {{ssn: row.ssn}}), (e:Email {{email: row.email}}),
      (p:Phone {{phoneNumber: row.phone_number}})
CREATE (c:Client {{id: row.id, name: row.name}}),
  (c)-[:HAS_SSN]->(s), (c)-[:HAS_EMAIL]->(e), (c)-[:HAS_PHONE]->(p);

LOAD CSV FROM "/csv/clients.csv" WITH HEADER AS row
WITH row WHERE row.is_mule = "1"
MATCH (s:SSN {{ssn: row.ssn}}), (e:Email {{email: row.email}}),
      (p:Phone {{phoneNumber: row.phone_number}})
CREATE (c:Client:Mule {{id: row.id, name: row.name}}),
  (c)-[:HAS_SSN]->(s), (c)-[:HAS_EMAIL]->(e), (c)-[:HAS_PHONE]->(p);

// Transactions, one pass per type, each creating PERFORMED and TO with the node.
{"".join(blocks)}
// NEXT chains, once every transaction exists.
{"".join(next_blocks)}
// FIRST_TX / LAST_TX, likewise.
LOAD CSV FROM "/csv/clients.csv" WITH HEADER AS row
WITH row WHERE row.first_tx_id <> ""
MATCH (c:Client {{id: row.id}}), (t:Transaction {{id: row.first_tx_id}})
CREATE (c)-[:FIRST_TX]->(t);

LOAD CSV FROM "/csv/clients.csv" WITH HEADER AS row
WITH row WHERE row.last_tx_id <> ""
MATCH (c:Client {{id: row.id}}), (t:Transaction {{id: row.last_tx_id}})
CREATE (c)-[:LAST_TX]->(t);

STORAGE MODE IN_MEMORY_TRANSACTIONAL;

CREATE CONSTRAINT ON (n:Bank) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Merchant) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:SSN) ASSERT n.ssn IS UNIQUE;
CREATE CONSTRAINT ON (n:Email) ASSERT n.email IS UNIQUE;
CREATE CONSTRAINT ON (n:Phone) ASSERT n.phoneNumber IS UNIQUE;
CREATE CONSTRAINT ON (n:Client) ASSERT n.id IS UNIQUE;
CREATE CONSTRAINT ON (n:Transaction) ASSERT n.id IS UNIQUE;
"""


def neo4j_loader():
    """LOAD CSV script for Neo4j 5.x and later, including Neo4j Desktop.

    Differences from the Memgraph script: constraints are created up front
    (Neo4j keeps them during a load, and the relationship passes need the
    indexes they bring), every write is wrapped in CALL { ... } IN TRANSACTIONS
    so no pass runs as one huge transaction, and empty CSV fields arrive as
    null rather than "".
    """
    tx_props = (
        "{ id: row.id,\n"
        "      amount: toFloat(row.amount),\n"
        "      fraud: row.fraud = '1',\n"
        "      step: toInteger(row.step),\n"
        "      globalStep: toInteger(row.global_step),\n"
        "      ts: toInteger(row.ts) }"
    )
    tx_blocks = []
    for kind in TX_TYPES:
        if kind == "Transfer":
            target = "MATCH (dst:Client {id: row.to_client_id})"
        elif kind == "Debit":
            target = "MATCH (dst:Bank {id: row.to_bank_id})"
        else:
            target = "MATCH (dst:Merchant {id: row.to_merchant_id})"
        tx_blocks.append(
            f"LOAD CSV WITH HEADERS FROM 'file:///transactions_{kind.lower()}.csv' AS row\n"
            "CALL { WITH row\n"
            "    MATCH (c:Client {id: row.client_id})\n"
            f"    {target}\n"
            f"    CREATE (c)-[:PERFORMED]->(t:Transaction:{kind} {tx_props})\n"
            "    CREATE (t)-[:TO]->(dst)\n"
            "} IN TRANSACTIONS OF 10000 ROWS;\n\n"
        )
    next_blocks = [
        f"LOAD CSV WITH HEADERS FROM 'file:///transactions_{kind.lower()}.csv' AS row\n"
        "CALL { WITH row\n"
        "    WITH row WHERE row.next_transaction_id IS NOT NULL\n"
        "    MATCH (a:Transaction {id: row.id})\n"
        "    MATCH (b:Transaction {id: row.next_transaction_id})\n"
        "    CREATE (a)-[:NEXT]->(b)\n"
        "} IN TRANSACTIONS OF 10000 ROWS;\n\n"
        for kind in TX_TYPES
    ]
    subtype_constraints = "\n".join(
        f"CREATE CONSTRAINT {kind.lower()}_id IF NOT EXISTS "
        f"FOR (n:{kind}) REQUIRE n.id IS UNIQUE;"
        for kind in TX_TYPES
    )
    return f"""\
// Synthetic fraud-detection dataset -> Neo4j 5.x+ (including Neo4j Desktop).
// Generated by scripts/generate_fraud_data.py.
//
// The CSVs must sit in the database's import folder, because 'file:///' is
// resolved there and nowhere else. In Neo4j Desktop: the DBMS's "..." menu ->
// Open folder -> Import, then copy the contents of csv/ into it (the files
// themselves, not the csv/ folder).
//
// Then run this whole file against an EMPTY database:
//   cypher-shell -u neo4j -p <password> -d <database> -f load_neo4j.cypher
// or paste it into Neo4j Browser one statement at a time.
//
// Expect this to take minutes rather than seconds -- LOAD CSV builds the graph
// transactionally, one row at a time.

{subtype_constraints}
CREATE CONSTRAINT transaction_id IF NOT EXISTS FOR (n:Transaction) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT client_id IF NOT EXISTS FOR (n:Client) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT merchant_id IF NOT EXISTS FOR (n:Merchant) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT bank_id IF NOT EXISTS FOR (n:Bank) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT ssn_unique IF NOT EXISTS FOR (n:SSN) REQUIRE n.ssn IS UNIQUE;
CREATE CONSTRAINT email_unique IF NOT EXISTS FOR (n:Email) REQUIRE n.email IS UNIQUE;
CREATE CONSTRAINT phone_unique IF NOT EXISTS FOR (n:Phone) REQUIRE n.phoneNumber IS UNIQUE;
CREATE INDEX transaction_global_step IF NOT EXISTS FOR (n:Transaction) ON (n.globalStep);

LOAD CSV WITH HEADERS FROM 'file:///banks.csv' AS row
CALL {{ WITH row CREATE (:Bank {{id: row.id, name: row.name}}) }} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///merchants.csv' AS row
CALL {{ WITH row CREATE (:Merchant {{id: row.id, name: row.name}}) }} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///ssns.csv' AS row
CALL {{ WITH row CREATE (:SSN {{ssn: row.ssn}}) }} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///emails.csv' AS row
CALL {{ WITH row CREATE (:Email {{email: row.email}}) }} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///phones.csv' AS row
CALL {{ WITH row CREATE (:Phone {{phoneNumber: row.phone_number}}) }} IN TRANSACTIONS OF 10000 ROWS;

// Clients load in two passes because a label cannot come from a CSV column.
LOAD CSV WITH HEADERS FROM 'file:///clients.csv' AS row
CALL {{ WITH row
    WITH row WHERE row.is_mule = '0'
    MATCH (s:SSN {{ssn: row.ssn}})
    MATCH (e:Email {{email: row.email}})
    MATCH (p:Phone {{phoneNumber: row.phone_number}})
    CREATE (c:Client {{id: row.id, name: row.name}})
    CREATE (c)-[:HAS_SSN]->(s)
    CREATE (c)-[:HAS_EMAIL]->(e)
    CREATE (c)-[:HAS_PHONE]->(p)
}} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///clients.csv' AS row
CALL {{ WITH row
    WITH row WHERE row.is_mule = '1'
    MATCH (s:SSN {{ssn: row.ssn}})
    MATCH (e:Email {{email: row.email}})
    MATCH (p:Phone {{phoneNumber: row.phone_number}})
    CREATE (c:Client:Mule {{id: row.id, name: row.name}})
    CREATE (c)-[:HAS_SSN]->(s)
    CREATE (c)-[:HAS_EMAIL]->(e)
    CREATE (c)-[:HAS_PHONE]->(p)
}} IN TRANSACTIONS OF 10000 ROWS;

// Transactions, one pass per type, each creating PERFORMED and TO with the node.
{"".join(tx_blocks)}// NEXT chains, once every transaction exists.
{"".join(next_blocks)}// FIRST_TX / LAST_TX, likewise.
LOAD CSV WITH HEADERS FROM 'file:///clients.csv' AS row
CALL {{ WITH row
    WITH row WHERE row.first_tx_id IS NOT NULL
    MATCH (c:Client {{id: row.id}})
    MATCH (t:Transaction {{id: row.first_tx_id}})
    CREATE (c)-[:FIRST_TX]->(t)
}} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV WITH HEADERS FROM 'file:///clients.csv' AS row
CALL {{ WITH row
    WITH row WHERE row.last_tx_id IS NOT NULL
    MATCH (c:Client {{id: row.id}})
    MATCH (t:Transaction {{id: row.last_tx_id}})
    CREATE (c)-[:LAST_TX]->(t)
}} IN TRANSACTIONS OF 10000 ROWS;
"""


if __name__ == "__main__":
    main()
