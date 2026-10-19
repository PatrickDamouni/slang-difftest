# Hostile plugin for testing the lint: it must be rejected (imports os, calls open).
import os
NAME = "example_evil"
KINDS = ["x"]
def generate(g, kind, depth): return open("/etc/passwd").read()
def to_slang(node, p): return ""
def evaluate(node, e, vmap): return 0
