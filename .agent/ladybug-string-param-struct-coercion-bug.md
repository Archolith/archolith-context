# Bug: Python 0.16.1: a STRING parameter whose value starts with `{`/`[` is silently coerced to STRUCT (lossy); regression from 0.15.3

## Summary

When a `str` parameter is passed to `Connection.execute(...)` and bound to a column
declared `STRING`, the binder inspects the value. If it begins with `{` or `[`, it
gets parsed as a STRUCT/MAP/LIST literal and the struct's text repr is stored
instead of the original string.

The caller passes a `str` and the target column is `STRING`, so the value should be
stored verbatim. Instead its content decides the bound type, and what lands in the
column is mutated and lossy. Three things get lost:

- string delimiters: `"curator_enabled"` becomes `curator_enabled`
- booleans get re-cased: `false` becomes `False`
- `null` is dropped: `[1, 2, null]` becomes `[1,2,]`

This is the silent version of #391, which only covered values that throw on `[]`
or `null`. #391 is closed-completed, but 0.16.1 now corrupts plain JSON objects too:
`{"k": "v"}` no longer round-trips. It did on 0.15.3 (see #391's repro, where
`{"k": "v"}` succeeded), so this is a regression in the 0.15.3 to 0.16.1 window.

## Environment

- `ladybug` 0.16.1 (via `pip install ladybug`)
- Python 3.13.12
- Windows 11
- Schema: single STRING column (also reproduced with a `JSON`-typed column, see below)

## Minimal reproduction

```python
import tempfile, os
import ladybug

tmp = tempfile.mkdtemp()
db = ladybug.Database(os.path.join(tmp, "repro.kuzu"))
conn = ladybug.Connection(db)
conn.execute("CREATE NODE TABLE T(id STRING PRIMARY KEY, cfg STRING)")

val = '{"curator_enabled": false, "x": [1, 2, null]}'

# 1. STRING column, str param -> SILENTLY MANGLED
conn.execute('CREATE (t:T {id: "a"}) SET t.cfg = $x', {"x": val})
print("STRING column   :", repr(conn.execute('MATCH (t:T {id:"a"}) RETURN t.cfg').get_next()[0]))
# -> '{curator_enabled: False, x: [1,2,]}'   (quotes dropped, false->False, null DROPPED)

# 2. Same value with one leading space -> stored VERBATIM
conn.execute('CREATE (t:T {id: "b"}) SET t.cfg = $x', {"x": " " + val})
print("leading space   :", repr(conn.execute('MATCH (t:T {id:"b"}) RETURN t.cfg').get_next()[0]))
# -> ' {"curator_enabled": false, "x": [1, 2, null]}'   (perfect round-trip)

# 3. cast(... AS STRING) / string() do NOT help -> corruption is at bind time, not eval time
conn.execute('CREATE (t:T {id: "c"}) SET t.cfg = cast($x AS STRING)', {"x": val})
print("cast AS STRING  :", repr(conn.execute('MATCH (t:T {id:"c"}) RETURN t.cfg').get_next()[0]))
# -> still '{curator_enabled: False, x: [1,2,]}'
```

The leading space is the giveaway: the same bytes round-trip once the value no
longer starts with `{`. `cast($x AS STRING)` and `string($x)` don't help either, so
the corruption happens in the parameter binder (prepare/bind), before query
evaluation.

## The `JSON` column type is not a workaround (it is worse)

Declaring the column `JSON`, as suggested on #391, loses data too and needs an
extension to read back:

```python
conn.execute("CREATE NODE TABLE J(id STRING PRIMARY KEY, cfg JSON)")
conn.execute('CREATE (j:J {id: "a"}) SET j.cfg = $x', {"x": '{"a": false, "x": [1, 2, null]}'})
print("JSON column     :", repr(conn.execute('MATCH (j:J {id:"a"}) RETURN j.cfg').get_next()[0]))
# -> '{a: False, x: [1,2,]}'   (unparseable struct repr; null DROPPED)

# to_json / json_quote / json_extract are not defined here:
# -> Catalog exception: function TO_JSON is not defined.
```

There's no in-engine way to store and read a JSON document losslessly through
parameter binding in 0.16.1.

## Suggested fix

Bind a Python `str` as `STRING` based on its Python type, not its content. If JSON
auto-detection is meant to be a feature, it shouldn't apply to a `str` targeting a
`STRING` column, and it shouldn't be lossy. Dropping `null` and re-casing booleans
is silent data corruption.

## Current workaround

base64-encode the JSON before binding and decode on read. The binder can't
reinterpret opaque bytes, so `null`, quotes, and casing all survive. It works, but
it makes the column unreadable in ad-hoc queries and shouldn't be necessary for a
`str` to `STRING` write.

## Related

- #391: STRING parameter binding fails ("vector ANY type") when the value parses
  as JSON containing `[]` or `null`. Closed-completed; covered the error cases, not
  the silent-mangle case here.
- #282: Python binding mutates deeply nested JSON values.
