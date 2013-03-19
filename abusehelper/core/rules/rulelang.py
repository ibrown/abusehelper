import re
import json

from . import atoms
from . import rules
from . import iprange

from .parsing import *


class Formatter(object):
    def __init__(self):
        self._types = {}

    def add(self, type_, func):
        self._types[type_] = func

    def handler(self, type_):
        def _formatter(func):
            self.add(type_, func)
            return func
        return _formatter

    def _format_gen(self, obj):
        type_ = type(obj)
        func = self._types.get(type_)
        if func is None:
            raise TypeError("can not format objects of type " + repr(type_))
        return func(self._format_gen, obj)

    def _format(self, obj):
        stack = [self._format_gen(obj)]

        while stack:
            try:
                value = stack[-1].next()
            except StopIteration:
                stack.pop()
                continue

            if not isinstance(value, basestring):
                stack.append(iter(value))
                continue

            yield value

    def format(self, obj):
        return "".join(self._format(obj))


formatter = Formatter()


unquoted_rex = re.compile(r"([^\s\\\(\)\"\*!=/]+)")
quoted_rex = re.compile(r'("(?:\\u[0-9a-fA-F]{4}|\\[\\"/fbnrt]|[^\\"])*")')


@parser_singleton
def string_parser((string, start, end)):
    if not start < end:
        yield None, None

    if string[start] == '"':
        match = quoted_rex.match(string, start, end)
        if not match:
            yield None, None
        value = json.loads(match.group(1))
        start = match.end()
    else:
        match = unquoted_rex.match(string, start, end)
        if not match:
            yield None, None
        value = match.group(1)
        start = match.end()

    yield None, (atoms.String(value), (string, start, end))


@formatter.handler(atoms.RegExp)
def format_regexp(format, regexp):
    escape_slash_rex = re.compile(r"((?:^|[^\\])(?:\\\\)*?)(/+)", re.U)

    def escape_slash(match):
        return match.group(1) + match.group(2).replace("/", "\\/")

    pattern = regexp.pattern
    pattern = escape_slash_rex.sub(escape_slash, pattern)

    result = "/" + pattern + "/"
    if regexp.ignore_case:
        result += "i"
    yield result


@formatter.handler(atoms.String)
def format_string(format, string):
    value = string.value
    match = unquoted_rex.match(value)
    if match and match.end() == len(value):
        yield value
    else:
        yield json.dumps(value)


ip_parser = transform(atoms.IP, iprange.IPRange.parser)


@formatter.handler(atoms.IP)
def format_ip(format, ip):
    yield unicode(ip.range)


star_parser = seq(txt("*"), epsilon(atoms.Star()), pick=1)


@formatter.handler(atoms.Star)
def format_star(format, _):
    return "*"


@parser_singleton
def regexp_parser(data, rex=re.compile(r'/((?:\\.|[^\\/])*)/(i)?')):
    string, start, end = data
    if not start < end:
        yield None, None

    if string[start] != "/":
        yield None, None

    match = rex.match(string, start, end)
    if not match:
        yield None, None

    ignore_case = match.group(2) is not None
    regexp = atoms.RegExp(match.group(1), ignore_case=ignore_case)
    yield None, (regexp, (string, match.end(), end))


@formatter.handler(rules.Fuzzy)
def format_fuzzy(format, fuzzy):
    yield format(fuzzy.atom)


@formatter.handler(rules.NonMatch)
def format_non_match(format, non_match):
    yield format(non_match.key)
    if isinstance(non_match.value, atoms.IP):
        yield " not in "
    else:
        yield "!="
    yield format(non_match.value)


@formatter.handler(rules.Match)
def format_match(format, match):
    yield format(match.key)
    if isinstance(match.value, atoms.IP):
        yield " in "
    else:
        yield "="
    yield format(match.value)


@formatter.handler(rules.And)
def format_and(format, rule):
    for index, subrule in enumerate(rule.subrules):
        if index != 0:
            yield " and "
        yield "("
        yield format(subrule)
        yield ")"


@formatter.handler(rules.Or)
def format_or(format, rule):
    for index, subrule in enumerate(rule.subrules):
        if index != 0:
            yield " or "
        yield "("
        yield format(subrule)
        yield ")"


@formatter.handler(rules.No)
def format_no(format, rule):
    yield "no "
    if not isinstance(rule.subrule, (rules.No, rules.Match, rules.NonMatch, rules.Fuzzy)):
        yield "("
        yield format(rule.subrule)
        yield ")"
    else:
        yield format(rule.subrule)


# Parsing

def _create_parser():
    expr = forward_ref()
    parens_expr = seq(txt("("), expr, txt(")"), pick=1)

    @parser_singleton
    def ws((string, start, end), chars=frozenset(" \t\n\r")):
        if not start < end or string[start] not in chars:
            yield None, None
        start += 1

        while start < end and string[start] in chars:
            start += 1
        yield None, (None, (string, start, end))

    match_tail = union(
        seq(maybe(ws), txt("="), maybe(txt("=")), maybe(ws), union(star_parser, regexp_parser, string_parser), pick=-1),
        seq(ws, txt("in", ignore_case=True), ws, ip_parser, pick=-1),
    )

    non_match_tail = union(
        seq(maybe(ws), txt("!="), maybe(ws), union(star_parser, regexp_parser, string_parser), pick=-1),
        seq(ws, txt("not", ignore_case=True), ws, txt("in", ignore_case=True), ws, ip_parser, pick=-1),
    )

    basic = union(
        step(
            star_parser,
            (match_tail, rules.Match),
            (non_match_tail, rules.NonMatch)
        ),

        transform(
            rules.Fuzzy,
            union(regexp_parser, ip_parser)
        ),

        step(
            string_parser,
            (match_tail, rules.Match),
            (non_match_tail, rules.NonMatch),
            step_default(rules.Fuzzy)
        )
    )

    no_parser = forward_ref()
    no_parser.set(transform(
        rules.No,
        seq(
            txt("no", ignore_case=True),
            union(
                seq(maybe(ws), parens_expr, pick=1),
                seq(ws, union(no_parser, basic), pick=1)
            ),
            pick=1
        )
    ))

    def binary_rule_tail(name):
        name_rule = txt(name, ignore_case=True)

        return transform(
            lambda (x, y): list(x) + [y],
            seq(
                name_rule,

                repeat(
                    seq(
                        union(
                            seq(maybe(ws), parens_expr, maybe(ws), pick=1),
                            seq(ws, union(no_parser, basic), ws, pick=1)
                        ),
                        name_rule,
                        pick=0
                    )
                ),

                union(
                    parens_expr,
                    seq(ws, expr, pick=1)
                ),

                pick=[1, 2]
            )
        )
    and_tail = binary_rule_tail("and")
    or_tail = binary_rule_tail("or")

    expr.set(
        seq(
            maybe(ws),
            union(
                step(
                    parens_expr,
                    (seq(maybe(ws), and_tail, pick=1), lambda x, y: rules.And(x, *y)),
                    (seq(maybe(ws), or_tail, pick=1), lambda x, y: rules.Or(x, *y)),
                    step_default()
                ),
                step(
                    union(no_parser, basic),
                    (seq(ws, and_tail, pick=1), lambda x, y: rules.And(x, *y)),
                    (seq(ws, or_tail, pick=1), lambda x, y: rules.Or(x, *y)),
                    step_default()
                )
            ),
            maybe(ws),
            pick=1
        )
    )
    return expr


expr = _create_parser()


def parse(string):
    match = expr.parse(string)
    if not match or match[1] != "":
        raise ValueError("could not parse " + repr(string))
    return match[0]


def rule(obj):
    if isinstance(obj, basestring):
        return parse(obj)
    return obj


def format(rule):
    return formatter.format(rule)
