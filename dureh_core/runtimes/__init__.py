"""Runtime adapters. Each adapter turns the runtime-neutral org/ definition plus
the installed startup package into runtime-specific files, and implements the
dispatch contract (spawn, stop, status, wait) with that runtime's primitives."""
