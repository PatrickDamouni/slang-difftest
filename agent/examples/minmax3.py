# Example plugin (hand-written, used to test the admission gate): min3/max3 via nested min/max.
NAME = "example_minmax3"
KINDS = ["min3", "max3"]


def generate(g, kind, depth):
    return g.node(kind, "v", [g.vec(depth), g.vec(depth), g.vec(depth)])


def to_slang(node, p):
    a, b, c = (p(k) for k in node.kids)
    f = "min" if node.kind == "min3" else "max"
    return f"{f}({f}({a}, {b}), {c})"


def evaluate(node, e, vmap):
    f = min if node.kind == "min3" else max
    return vmap(lambda x, y, z: f(f(x, y), z), *(e(k) for k in node.kids))
