# Deliberately wrong reference (says max where Slang says min): the gate must reject it.
NAME = "example_bad_ref"
KINDS = ["badmin"]


def generate(g, kind, depth):
    return g.node(kind, "v", [g.vec(depth), g.vec(depth)])


def to_slang(node, p):
    return f"min({p(node.kids[0])}, {p(node.kids[1])})"


def evaluate(node, e, vmap):
    return vmap(max, e(node.kids[0]), e(node.kids[1]))
