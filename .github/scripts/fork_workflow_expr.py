"""Fork only: GitHub Actions expressions evaluated as GitHub does, for the fork workflow tests
(fork-contracts.test.py, fork-owned-paths.test.py). Not run by any workflow."""

import re


def gh_eval(text, ctx, raw=False):
    """A GitHub Actions expression, as GitHub evaluates it, for the few forms fork workflows use:
    literals, contexts (a missing property is null), !, ==, != (strings ignore case), &&, || (each
    gives one of its operands), parentheses, format(), startsWith() and the status functions success(),
    failure(), cancelled() and always() (ctx["status"]). Anything else raises ValueError. True or false, or with
    raw the value itself (what a ${{ }} inside a string puts there)."""
    tokens = re.findall(r"'(?:[^']|'')*'|[A-Za-z_][\w.-]*|\d+|==|!=|&&|\|\||[!(),]|\S", text)
    pos = 0

    def truthy(v):
        return v not in (None, False, 0, "")

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take(expected=None):
        nonlocal pos
        if pos >= len(tokens) or (expected and tokens[pos] != expected):
            raise ValueError(f"expected {expected or 'more'} in {text!r}")
        pos += 1
        return tokens[pos - 1]

    def primary():
        tok = take()
        if tok == "(":
            v = disjunction()
            take(")")
            return v
        if tok == "!":
            return not truthy(primary())
        if tok.startswith("'"):
            return tok[1:-1].replace("''", "'")
        if tok.isdigit():
            return int(tok)
        if tok in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[tok]
        if not re.fullmatch(r"[A-Za-z_][\w.-]*", tok):
            raise ValueError(f"cannot read {tok!r} in {text!r}")
        if peek() == "(":
            take("(")
            args = []
            while peek() != ")":
                args.append(disjunction())
                if peek() == ",":
                    take(",")
            take(")")
            if tok == "format":
                return re.sub(r"\{(\d+)\}", lambda m: str(args[int(m.group(1)) + 1]), str(args[0]))
            if tok == "startsWith":
                return str(args[0]).lower().startswith(str(args[1]).lower())
            # A job's status functions: ctx["status"] is "success" (the default), "failure" or "cancelled".
            if tok in ("success", "failure", "cancelled", "always") and not args:
                status = ctx.get("status", "success")
                return tok == "always" or status == tok
            raise ValueError(f"function {tok} is not known here")
        value = ctx
        for part in tok.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def comparison():
        left = primary()
        while peek() in ("==", "!="):
            op = take()
            right = primary()
            a, b = (x.lower() if isinstance(x, str) else x for x in (left, right))
            left = (a == b) if op == "==" else (a != b)
        return left

    def conjunction():
        left = comparison()
        while peek() == "&&":
            take()
            right = comparison()
            left = right if truthy(left) else left
        return left

    def disjunction():
        left = conjunction()
        while peek() == "||":
            take()
            right = conjunction()
            left = left if truthy(left) else right
        return left

    value = disjunction()
    if pos != len(tokens):
        raise ValueError(f"cannot read the end of {text!r}")
    return value if raw else truthy(value)
