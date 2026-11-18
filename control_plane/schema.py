from sqlalchemy import Column, DateTime, MetaData, String, Table, func

metadata = MetaData()

Table(
    "workspaces",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), server_default=func.now(), nullable=False
    ),
)
