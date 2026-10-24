"""Scripts that run inside the runner container to read an account's quota without calling the model.

They use only the standard library, because lastcall is not installed in the runner: the engine sends the script
as a /ctx file and runs it with the runner's Python. Each script takes the CLI command as arguments, so tests can
replace the CLI with a fake one.
"""
