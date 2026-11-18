"""Translate metadata from explicitly configured local service targets."""

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from shared.contracts import TOOLS, tool_contract
from shared.protos.it_pb2 import DESCRIPTOR

PRIVATE = [
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
]


@dataclass(frozen=True)
class Target:
    protocol: str
    port: int
    secret: str

    def location(self, host: str) -> str:
        return f"{host}:{self.port}"


async def resolve(host: str, port: int) -> str:
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, family=socket.AF_INET
    )
    ips = {ipaddress.ip_address(item[4][0]) for item in addresses}
    if not ips or any(not any(ip in net for net in PRIVATE) for ip in ips):
        raise ValueError("TARGET_ADDRESS_NOT_ALLOWED")
    return str(sorted(ips)[0])


def normalize(value, components, depth=0):
    if depth > 14:
        raise ValueError("SCHEMA_TOO_DEEP")
    if isinstance(value, list):
        return [normalize(item, components, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        prefix = "#/components/schemas/"
        if (
            not isinstance(ref, str)
            or not ref.startswith(prefix)
            or ref[len(prefix) :] not in components
        ):
            raise ValueError("UNSUPPORTED_SCHEMA_REFERENCE")
        return normalize(components[ref[len(prefix) :]], components, depth + 1)
    return {
        key: normalize(item, components, depth + 1)
        for key, item in value.items()
        if key not in {"title", "description", "examples"}
    }


def simple_object(schema: dict) -> None:
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise ValueError("UNSUPPORTED_SCHEMA")
    properties = schema.get("properties", {})
    if (
        set(schema) - {"type", "properties", "required", "additionalProperties"}
        or not properties
        or len(properties) > 16
    ):
        raise ValueError("UNSUPPORTED_SCHEMA")
    if set(schema.get("required", [])) - set(properties):
        raise ValueError("UNSUPPORTED_SCHEMA")
    for field in properties.values():
        if field.get("type") not in {"string", "integer", "boolean"} or set(field) - {
            "type",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "pattern",
        }:
            raise ValueError("UNSUPPORTED_SCHEMA")
        if "pattern" in field and field["pattern"] != "^[A-Za-z0-9-]+$":
            raise ValueError("UNSUPPORTED_SCHEMA")


async def read_json(url, headers) -> dict:
    async with httpx.AsyncClient(
        timeout=3, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(8192):
                body.extend(chunk)
                if len(body) > 262144:
                    raise ValueError("METADATA_TOO_LARGE")
    return json.loads(body)


async def fetch(host: str, target: Target) -> tuple[dict, list[str]]:
    address = await resolve(host, target.port)
    location = f"{address}:{target.port}"
    headers = {
        "Authorization": f"Bearer {target.secret}",
        "Host": target.location(host),
        "Accept-Encoding": "identity",
    }
    collected = {}
    skipped = []
    if target.protocol == "REST":
        source = await read_json(f"http://{location}/openapi.json", headers)
        if not str(source.get("openapi", "")).startswith("3.1"):
            raise ValueError("UNSUPPORTED_OPENAPI")
        components = source.get("components", {}).get("schemas", {})
        for path, operations in source.get("paths", {}).items():
            identifier = f"{host}.{path.removeprefix('/')}"
            if identifier not in TOOLS and identifier != "inventory.get_inventory":
                skipped.append(path)
                continue
            try:
                operation = operations["post"]
                inp = normalize(
                    operation["requestBody"]["content"]["application/json"]["schema"],
                    components,
                )
                out = normalize(
                    operation["responses"]["200"]["content"]["application/json"][
                        "schema"
                    ],
                    components,
                )
                collected[identifier] = {"input_schema": inp, "output_schema": out}
            except (KeyError, ValueError, TypeError):
                skipped.append(path)
    elif target.protocol == "MCP":
        from gateway.runtime import bounded_client

        transport = StreamableHttpTransport(
            f"http://{location}/mcp",
            headers=headers,
            httpx_client_factory=bounded_client,
        )
        async with Client(transport, timeout=4) as client:
            for item in await client.list_tools():
                identifier = f"{host}.{item.name}"
                if identifier not in TOOLS or item.outputSchema is None:
                    skipped.append(item.name)
                    continue
                collected[identifier] = {
                    "input_schema": item.inputSchema,
                    "output_schema": item.outputSchema,
                }
    else:
        from gateway.grpc_adapter import probe_it

        await probe_it(address, target.secret)
        names = {
            "CheckServiceHealth": "check_service_health",
            "ListIncidents": "list_incidents",
            "CreateIncident": "create_incident",
        }
        for method in DESCRIPTOR.services_by_name["Operations"].methods:
            identifier = f"it.{names[method.name]}"
            contract = tool_contract(identifier)
            if set(contract["input_schema"]["properties"]) != set(
                method.input_type.fields_by_name
            ):
                skipped.append(method.name)
                continue
            collected[identifier] = {
                key: contract[key] for key in ("input_schema", "output_schema")
            }
    return collected, skipped
