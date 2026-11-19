"""Add HR and Analytics, complete domain state, and persist governance settings."""

import json
import os
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0007"
down_revision = "0006"


def upgrade():
    conn = op.get_bind()
    for domain in ("hr", "analytics"):
        password = os.environ[f"TOOLMESH_{domain.upper()}_DB_PASSWORD"]
        role = f"toolmesh_{domain}"
        statement = conn.execute(
            text(
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L',CAST(:role AS text),CAST(:password AS text))"
            ),
            {"role": role, "password": password},
        ).scalar_one()
        op.execute(statement)
        op.execute(f"GRANT CONNECT ON DATABASE toolmesh TO {role}")
        op.execute(f"CREATE SCHEMA {domain}")
        op.execute(f"GRANT USAGE ON SCHEMA {domain} TO {role}")

    for statement in (
        "ALTER TABLE payments.transactions ADD COLUMN captured_on date NOT NULL DEFAULT DATE '2026-11-01'",
        "UPDATE payments.transactions SET captured_on=DATE '2026-10-01' WHERE order_id='O-103'",
        """CREATE TABLE payments.methods (
            transaction_id text PRIMARY KEY REFERENCES payments.transactions,
            brand text NOT NULL, last_four text NOT NULL CHECK(last_four ~ '^[0-9]{4}$')
        )""",
        "INSERT INTO payments.methods SELECT id,'Sandbox Visa','4242' FROM payments.transactions",
        "GRANT SELECT ON payments.methods TO toolmesh_payments",
        """CREATE TABLE support.customers (customer_id text PRIMARY KEY,display_name text NOT NULL,status text NOT NULL)""",
        "INSERT INTO support.customers VALUES ('C-102','River Demo','active'),('C-104','Hill Demo','active')",
        """CREATE TABLE support.ticket_history (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,ticket_id uuid NOT NULL REFERENCES support.tickets,
            status text NOT NULL,note text NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
        )""",
        """INSERT INTO support.ticket_history(ticket_id,status,note)
           SELECT id,status,'State at migration' FROM support.tickets""",
        """CREATE FUNCTION support.record_ticket_state() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
             IF TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status THEN
               INSERT INTO support.ticket_history(ticket_id,status,note)
               VALUES(NEW.id,NEW.status,coalesce(nullif(current_setting('toolmesh.ticket_note',true),''),'Status recorded'));
             END IF;
             RETURN NEW;
           END $$""",
        """CREATE TRIGGER ticket_state AFTER INSERT OR UPDATE OF status ON support.tickets
            FOR EACH ROW EXECUTE FUNCTION support.record_ticket_state()""",
        "GRANT SELECT ON support.customers TO toolmesh_support",
        "GRANT INSERT ON support.ticket_history TO toolmesh_support",
        """CREATE TABLE it.logs (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, service_id text NOT NULL
            REFERENCES it.services, level text NOT NULL,message text NOT NULL,logged_at timestamptz NOT NULL DEFAULT now())""",
        """INSERT INTO it.logs(service_id,level,message) VALUES
             ('sandbox-api','ERROR','Synthetic request deadline exceeded'),('sandbox-api','WARN','Synthetic latency threshold crossed')""",
        """CREATE TABLE it.deployments (service_id text PRIMARY KEY REFERENCES it.services, release text NOT NULL,status text NOT NULL)""",
        "INSERT INTO it.deployments SELECT id,'sandbox-2026.11',status FROM it.services",
        """CREATE TABLE it.restarts (id uuid PRIMARY KEY,service_id text NOT NULL REFERENCES it.services,
            customer_id text NOT NULL,status text NOT NULL,created_at timestamptz NOT NULL DEFAULT now())""",
        "GRANT SELECT ON it.logs,it.deployments,it.restarts TO toolmesh_it",
        "GRANT INSERT ON it.logs,it.restarts TO toolmesh_it",
        "GRANT UPDATE (status) ON it.services,it.deployments TO toolmesh_it",
        "ALTER TABLE communication.messages ALTER COLUMN refund_id DROP NOT NULL",
        "ALTER TABLE communication.messages ADD COLUMN kind text NOT NULL DEFAULT 'notification'",
        "ALTER TABLE communication.messages ADD COLUMN subject text NOT NULL DEFAULT 'Sandbox refund update'",
        "ALTER TABLE communication.messages ADD COLUMN recipient_id text",
        "ALTER TABLE communication.messages ADD COLUMN leave_id uuid UNIQUE",
        """CREATE TABLE communication.channels (customer_id text NOT NULL,channel_name text NOT NULL,
           members text[] NOT NULL, PRIMARY KEY(customer_id,channel_name))""",
        "GRANT SELECT,INSERT ON communication.channels TO toolmesh_communication",
        """CREATE TABLE hr.departments (id text PRIMARY KEY,customer_id text NOT NULL,name text NOT NULL)""",
        """CREATE TABLE hr.employees (id text PRIMARY KEY,customer_id text NOT NULL,
            department_id text NOT NULL REFERENCES hr.departments,display_name text NOT NULL,title text NOT NULL,
            manager_id text REFERENCES hr.employees,available_days int NOT NULL CHECK(available_days>=0))""",
        """CREATE TABLE hr.policies (id text PRIMARY KEY,customer_id text NOT NULL,title text NOT NULL,summary text NOT NULL)""",
        """CREATE TABLE hr.leave_requests (id uuid PRIMARY KEY,employee_id text NOT NULL REFERENCES hr.employees,
            start_date date NOT NULL,end_date date NOT NULL,days int NOT NULL,status text NOT NULL DEFAULT 'SUBMITTED')""",
        "INSERT INTO hr.departments VALUES ('D-102','C-102','Operations'),('D-104','C-104','Research')",
        """INSERT INTO hr.employees VALUES ('E-103','C-102','D-102','Taylor Demo','Manager',NULL,20),
             ('E-102','C-102','D-102','Casey Demo','Operator','E-103',15),
             ('E-104','C-104','D-104','Morgan Demo','Researcher',NULL,18)""",
        "INSERT INTO hr.policies VALUES ('LEAVE','C-102','Annual leave','Requests reserve calendar days from the available allowance.')",
        "GRANT SELECT ON ALL TABLES IN SCHEMA hr TO toolmesh_hr",
        "GRANT INSERT ON hr.leave_requests TO toolmesh_hr",
        "GRANT UPDATE (available_days) ON hr.employees TO toolmesh_hr",
        """CREATE VIEW analytics.captures AS SELECT customer_id,captured_on,amount_minor,currency
           FROM payments.transactions WHERE status='captured'""",
        """CREATE VIEW analytics.refunds AS SELECT customer_id,(created_at AT TIME ZONE 'UTC')::date AS day,amount_minor,currency
           FROM payments.refunds""",
        """CREATE TABLE analytics.reports (id uuid PRIMARY KEY,customer_id text NOT NULL,
           start_date date NOT NULL,end_date date NOT NULL,sales jsonb NOT NULL,refunds jsonb NOT NULL,
           created_at timestamptz NOT NULL DEFAULT now())""",
        "GRANT SELECT ON analytics.captures,analytics.refunds TO toolmesh_analytics",
        "GRANT SELECT,INSERT ON analytics.reports TO toolmesh_analytics",
        """CREATE TABLE agent_accounts (id text PRIMARY KEY,profile text NOT NULL,enabled boolean NOT NULL DEFAULT true,
           credential_hash text,origin text NOT NULL DEFAULT 'managed')""",
        """INSERT INTO agent_accounts(id,profile,origin) VALUES ('support-agent','support','environment'),
           ('finance-agent','finance','environment'),('it-agent','it','environment'),
           ('hr-agent','hr','environment'),('payments-agent','payments','environment')""",
        """CREATE TABLE policy_rules (id uuid PRIMARY KEY,principal text NOT NULL,tool_id text NOT NULL,
            customer_id text NOT NULL,effect text NOT NULL CHECK(effect IN ('allow','deny')),enabled boolean NOT NULL,
            version int NOT NULL,UNIQUE(principal,tool_id,customer_id,effect))""",
        """CREATE TABLE policy_revisions (policy_id uuid NOT NULL REFERENCES policy_rules,version int NOT NULL,
           actor text NOT NULL,reason text NOT NULL,enabled boolean NOT NULL,changed_at timestamptz NOT NULL DEFAULT now(),
           PRIMARY KEY(policy_id,version))""",
        """CREATE TABLE quota_settings (id boolean PRIMARY KEY DEFAULT true CHECK(id),
           per_tool int NOT NULL CHECK(per_tool BETWEEN 1 AND 600),global_limit int NOT NULL CHECK(global_limit BETWEEN 1 AND 10000))""",
        "INSERT INTO quota_settings VALUES (true,30,300)",
        "GRANT SELECT,INSERT,UPDATE ON agent_accounts,policy_rules TO toolmesh_app",
        "GRANT SELECT,INSERT ON policy_revisions TO toolmesh_app",
        "GRANT SELECT,UPDATE ON quota_settings TO toolmesh_app",
        """INSERT INTO services(id,name,protocol,owner) VALUES ('hr','Human Resources','REST','platform-admin'),
           ('analytics','Analytics','REST','platform-admin'),('it','IT Operations','gRPC','platform-admin') ON CONFLICT DO NOTHING""",
    ):
        op.execute(statement)

    manifests = json.loads(Path(__file__).with_name("0007_tools.json").read_text())
    for tool_id, contract in manifests.items():
        conn.execute(
            text("INSERT INTO tools(id,service_id) VALUES (:id,:service)"),
            {"id": tool_id, "service": tool_id.split(".")[0]},
        )
        conn.execute(
            text(
                "INSERT INTO tool_versions(tool_id,version,contract) VALUES (:id,1,CAST(:contract AS jsonb))"
            ),
            {"id": tool_id, "contract": json.dumps(contract)},
        )


def downgrade():
    raise RuntimeError("Reverse this migration by restoring a verified backup.")
