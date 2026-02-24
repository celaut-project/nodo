from protos import celaut_pb2

from src.commands.execute import resolve_service_hash
from src.utils.cost_functions.generate_estimated_cost import (
    generate_estimated_cost,
    get_resource_availability,
)
from src.utils.utils import (
    from_gas_amount,
    read_metadata_from_disk,
    read_service_from_disk,
    to_gas_amount,
)

ESTIMATION_INITIAL_GAS_AMOUNT = 10 ** 16


def _format_size(value_in_bytes: int) -> str:
    if value_in_bytes < 1024:
        return f"{value_in_bytes} B"
    if value_in_bytes < 1024 ** 2:
        return f"{value_in_bytes / 1024:.2f} KB"
    if value_in_bytes < 1024 ** 3:
        return f"{value_in_bytes / (1024 ** 2):.2f} MB"
    if value_in_bytes < 1024 ** 4:
        return f"{value_in_bytes / (1024 ** 3):.2f} GB"
    return f"{value_in_bytes / (1024 ** 4):.2f} TB"


def _print_resource_availability(resource_info: dict) -> None:
    print("Resource availability:")
    print(
        f"- CPU: {resource_info['system_cpu_available_percent']:.2f}% available "
        f"(total physical cores: {resource_info['system_cpu_total']})"
    )
    print(
        f"- RAM (system): {_format_size(resource_info['system_memory_available'])} available "
        f"/ {_format_size(resource_info['system_memory_total'])} total"
    )
    print(
        f"- RAM (service pool): {_format_size(resource_info['service_memory_pool_available'])} available "
        f"/ {_format_size(resource_info['service_memory_pool_total'])} total"
    )
    print(
        f"- Disk '/': {_format_size(resource_info['system_disk_free'])} free "
        f"/ {_format_size(resource_info['system_disk_total'])} total"
    )
    if resource_info["requested_mem_limit"] > 0:
        print(
            f"- Requested max memory (service.at_most.mem_limit): "
            f"{_format_size(resource_info['requested_mem_limit'])}"
        )


def estimate(service: str) -> None:
    service = resolve_service_hash(service)
    if not service:
        print("No service allowed.")
        return

    metadata = read_metadata_from_disk(service_hash=service)
    if not metadata:
        print(f"Cannot estimate cost. Metadata for service {service} not found.")
        return

    service_definition = read_service_from_disk(service_hash=service)
    if not service_definition:
        print(f"Cannot estimate cost. Service {service} not found on local registry.")
        return

    resources = service_definition.container.resources
    resource_info = get_resource_availability(resources=resources)

    print(f"Estimate {service}")
    _print_resource_availability(resource_info=resource_info)

    if not resource_info["can_execute"]:
        print("Execution feasibility: NO")
        print(f"Reason: {resource_info['reason']}")
        return

    configuration = celaut_pb2.Configuration(
        initial_gas_amount=to_gas_amount(gas_amount=ESTIMATION_INITIAL_GAS_AMOUNT)
    )

    try:
        estimated_cost = generate_estimated_cost(
            metadata=metadata,
            config=configuration,
            resources=resources,
        )
    except Exception as e:
        print("Execution feasibility: NO")
        print(f"Reason: {str(e)}")
        return

    if not estimated_cost:
        print("Execution feasibility: NO")
        print("Reason: could not generate a valid estimated cost.")
        return

    print("Execution feasibility: YES")
    print("Estimated costs (gas units):")
    print(f"- Initial cost: {from_gas_amount(estimated_cost.cost)}")
    print(f"- Initial maintenance: {from_gas_amount(estimated_cost.init_maintenance_cost)}")
    print(f"- Max maintenance: {from_gas_amount(estimated_cost.max_maintenance_cost)}")
    print(f"- Maintenance loop (seconds): {estimated_cost.maintenance_seconds_loop}")
