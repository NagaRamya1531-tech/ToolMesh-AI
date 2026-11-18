from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CustomerRequest(_message.Message):
    __slots__ = ("customer_id",)
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    customer_id: str
    def __init__(self, customer_id: _Optional[str] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ("customer_id", "service_id")
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    customer_id: str
    service_id: str
    def __init__(self, customer_id: _Optional[str] = ..., service_id: _Optional[str] = ...) -> None: ...

class ServiceHealth(_message.Message):
    __slots__ = ("service_id", "status")
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    service_id: str
    status: str
    def __init__(self, service_id: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class IncidentRequest(_message.Message):
    __slots__ = ("customer_id", "service_id", "title")
    CUSTOMER_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    customer_id: str
    service_id: str
    title: str
    def __init__(self, customer_id: _Optional[str] = ..., service_id: _Optional[str] = ..., title: _Optional[str] = ...) -> None: ...

class Incident(_message.Message):
    __slots__ = ("id", "service_id", "title", "status")
    ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    service_id: str
    title: str
    status: str
    def __init__(self, id: _Optional[str] = ..., service_id: _Optional[str] = ..., title: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class IncidentList(_message.Message):
    __slots__ = ("incidents",)
    INCIDENTS_FIELD_NUMBER: _ClassVar[int]
    incidents: _containers.RepeatedCompositeFieldContainer[Incident]
    def __init__(self, incidents: _Optional[_Iterable[_Union[Incident, _Mapping]]] = ...) -> None: ...
