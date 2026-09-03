from analysis.registry import MethodRegistry


errors = MethodRegistry().validate_library()
if errors:
    raise SystemExit("\n".join(errors))
print("METHOD_LIBRARY_OK")

