from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
]
Subject = Annotated[str, Field(min_length=1, max_length=160)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Customer(Contract):
    customer_id: Identifier
