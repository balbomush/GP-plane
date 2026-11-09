"""Strict input shapes shared by search requests and persisted GP jobs."""

from typing import Annotated, Literal, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from pydantic_core import PydanticCustomError

from ..discovery_domains import classify_domain_input


class StrictRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


PositiveInt = Annotated[int, Field(ge=1)]
def _sqlite_id(value: int) -> int:
    if value >= 2**63:
        raise PydanticCustomError("less_than", "SQLite ID is outside the supported range")
    return value


DomainId = Annotated[int, Field(ge=1), AfterValidator(_sqlite_id)]
DomainText = Annotated[str, StringConstraints(min_length=1)]
Domains = Annotated[list[DomainText], Field(min_length=1, json_schema_extra={"uniqueItems": True})]


class BlockcheckStockParameters(StrictRequest):
    include_quic: bool = True
    enable_http: bool = False
    enable_tls12: bool = True
    enable_tls13: bool = False
    enable_ipv6: bool = False
    scan_level: Literal["quick", "standard", "force"] = "standard"
    repeats: PositiveInt = 1
    repeat_parallel: bool = False
    skip_dnscheck: bool = True
    skip_ipblock: bool = True
    curl_max_time: PositiveInt = 2
    curl_max_time_quic: PositiveInt = 2
    curl_max_time_doh: PositiveInt = 2
    debug_stdout: bool = False


class BlockcheckGpParameters(BlockcheckStockParameters):
    curl_parallelism: Annotated[int, Field(ge=1, le=10)] = 4


class SearchDomains(StrictRequest):
    domains: Domains
    # An omitted ID has no preset. Explicit JSON null remains invalid.
    domain_list_id: DomainId = Field(default_factory=lambda: cast(int, None))


class BlockcheckStockStartRequest(SearchDomains):
    parameters: BlockcheckStockParameters = Field(
        default_factory=BlockcheckStockParameters, json_schema_extra={"default": {}},
    )


class BlockcheckGpStartRequest(SearchDomains):
    parameters: BlockcheckGpParameters = Field(
        default_factory=BlockcheckGpParameters, json_schema_extra={"default": {}},
    )


PROTOCOLS = ("include_quic", "enable_http", "enable_tls12", "enable_tls13")


def has_protocol(parameters: dict) -> bool:
    return any(parameters.get(key, BlockcheckStockParameters.model_fields[key].default) for key in PROTOCOLS)


def valid_saved_gp(domains: object, parameters: object) -> bool:
    try:
        spec = BlockcheckGpStartRequest.model_validate({"domains": domains, "parameters": parameters})
    except ValidationError:
        return False
    return (
        spec.parameters.model_dump() == parameters and has_protocol(spec.parameters.model_dump())
        and len(set(spec.domains)) == len(spec.domains)
        and all(
            (entry := classify_domain_input(domain))["valid"] and entry["domain"] == domain
            for domain in spec.domains
        )
    )
