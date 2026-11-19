import grpc
from fastapi import HTTPException
from google.protobuf.json_format import MessageToDict

from shared.protos import it_pb2 as pb
from shared.protos.it_pb2_grpc import OperationsStub

METHODS = {
    "check_service_health": ("CheckServiceHealth", pb.HealthRequest),
    "list_incidents": ("ListIncidents", pb.CustomerRequest),
    "create_incident": ("CreateIncident", pb.IncidentRequest),
    "get_system_logs": ("GetSystemLogs", pb.LogRequest),
    "restart_service": ("RestartService", pb.RestartRequest),
    "get_restart_receipt": ("GetRestartReceipt", pb.RestartRequest),
    "get_deployment_status": ("GetDeploymentStatus", pb.HealthRequest),
}


async def call_it(address: str, secret: str, operation: str, args: dict) -> dict:
    method, request = METHODS[operation]
    options = [
        ("grpc.max_receive_message_length", 65536),
        ("grpc.max_send_message_length", 32768),
    ]
    try:
        async with grpc.aio.insecure_channel(
            f"{address}:50051", options=options
        ) as channel:
            result = await getattr(OperationsStub(channel), method)(
                request(**args),
                metadata=(("authorization", f"Bearer {secret}"),),
                timeout=3,
            )
        return MessageToDict(
            result,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
    except grpc.aio.AioRpcError as exc:
        code = exc.code()
        if code == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(404, "RECORD_NOT_AVAILABLE") from None
        if code == grpc.StatusCode.ALREADY_EXISTS:
            raise HTTPException(409, "RESTART_CONFLICT") from None
        if code == grpc.StatusCode.INVALID_ARGUMENT:
            raise HTTPException(422, "INVALID_ARGUMENT") from None
        if code == grpc.StatusCode.DEADLINE_EXCEEDED:
            raise HTTPException(504, "UPSTREAM_TIMEOUT") from None
        if code in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.RESOURCE_EXHAUSTED}:
            raise HTTPException(503, "UPSTREAM_UNAVAILABLE") from None
        raise HTTPException(502, "UPSTREAM_ERROR") from None


async def probe_it(address: str, secret: str) -> None:
    await call_it(
        address,
        secret,
        "check_service_health",
        {"customer_id": "C-102", "service_id": "sandbox-api"},
    )
