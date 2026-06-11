from typing import Any
from tools.definitions import ToolDefinition


def validate_params(tool: ToolDefinition, params: dict) -> tuple[bool, str]:
    """
    Validate params against the tool's JSON Schema.
    Returns (is_valid, error_message).
    """
    schema = tool.parameters_schema
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    # Check required fields
    missing = [f for f in required if f not in params and f not in ("__confirmed__",)]
    if missing:
        return False, f"Missing required parameters: {', '.join(missing)}"

    # Check type and enum constraints for each provided param
    for field_name, field_schema in properties.items():
        if field_name not in params:
            continue
        value = params[field_name]

        # Enum check
        if "enum" in field_schema and value not in field_schema["enum"]:
            return False, (
                f"Invalid value '{value}' for '{field_name}'. "
                f"Allowed: {field_schema['enum']}"
            )

        # Type check
        expected = field_schema.get("type")
        if expected == "integer":
            # In Python, bool is a subclass of int — reject booleans explicitly
            if isinstance(value, bool) or not isinstance(value, int):
                return False, (
                    f"'{field_name}' must be an integer, got {type(value).__name__}"
                )
        elif expected == "string":
            if not isinstance(value, str):
                return False, (
                    f"'{field_name}' must be a string, got {type(value).__name__}"
                )
        elif expected == "boolean":
            if not isinstance(value, bool):
                return False, (
                    f"'{field_name}' must be a boolean, got {type(value).__name__}"
                )
        elif expected == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, (
                    f"'{field_name}' must be a number, got {type(value).__name__}"
                )
        elif expected == "array":
            if not isinstance(value, list):
                return False, (
                    f"'{field_name}' must be an array, got {type(value).__name__}"
                )

    return True, ""
