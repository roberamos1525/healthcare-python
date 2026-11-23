# Hardest Challenge: Making FastAPI and Pydantic Answer Exactly Like the Reference

## The problem

The Python backend has to be a drop-in replacement for the reference implementation: the same API
contract, the same database and the same observable behaviour, including for clients that send
something wrong. The web application shows validation messages next to form fields by their key, the
contract tests compare problem documents, and a request may be retried against another backend that
shares the database. So a malformed or incomplete request must get the same status, the same keys and
the same messages, not merely "a 400".

Booking under contention, tenant isolation and DST-correct slots were demanding too, but they rest on
PostgreSQL features and standard-library modules that behave the same from every language (advisory
locks, an exclusion constraint, row-level security with transaction-local settings, `zoneinfo`). The
HTTP boundary was harder in Python than in the other ports for a specific reason: **FastAPI's central
feature is exactly what had to be switched off**. FastAPI binds and validates requests with Pydantic
and answers with its own 422 document; the reference's behaviour is the emergent result of ASP.NET Core
model binding, System.Text.Json, nullable reference-type metadata, MVC's automatic 400 and
FluentValidation, and its routing comes from ASP.NET Core's routing tree. The defaults differ in almost
every case that matters:

| Request (to `POST /api/v1/auth/register` unless noted) | Reference | FastAPI with a Pydantic model |
|---|---|---|
| `{}` | 400 `validation_failed`, `email: ["The Email field is required."]` … for all four properties; the request's rules do **not** run | 422, `detail: [{"type": "missing", "loc": ["body", "email"], "msg": "Field required"}, …]` |
| `{"email": null, …}` | Same as absent: `The Email field is required.` | 422 `string_type` ("Input should be a valid string") |
| `{"defaultAppointmentDurationMinutes": "30"}` (settings) | 400, key `defaultAppointmentDurationMinutes`: `The value is missing or not valid.` plus `request: ["The request field is required."]` | Accepted as 30 (lax mode) |
| `30.0` for an `int` | Rejected the same way | Accepted as 30 (lax mode); strict mode rejects it, but strict mode also rejects `"2026-11-25"` for a `date` in Python mode |
| `{"acceptingNewPatients": null}` (bool) | Rejected at that key, plus the `request` entry | 422 with Pydantic wording |
| `{"specialties": [{}]}` | 400 at `specialties[0].Name` — PascalCase, from a different stage than the type errors | 422 at `loc: ["body", "specialties", 0, "name"]` |
| `{"FIRSTNAME": "Sam"}` | Bound (case-insensitive) | Ignored, then "Field required" |
| `{"email": "a", "email": "b"}` | Last value wins | Last value wins (the one agreement) |
| `{"startTime": "8am"}` or `null` | `Times must use the HH:mm format.` | `time_parsing` / `time_type` messages |
| `{"gender": 0}` or an absent enum | Rejected / takes the enum's first member | Accepted if 0 is a value / "Field required" |
| `{"email": ` (truncated) | 400 at key `email` | 422 `json_invalid` with a character position |
| `GET /api/v1/patients/self-registration` | 405, `Allow: POST` | 405 from Starlette with `Allow: POST` here, but `GET /patients/not-a-guid` is routed to the GET handler |
| `DELETE /api/v1/patients/not-a-guid` | 405 `Allow: GET, PUT` (decided before the GUID constraint) | 404 with a `uuid` path convertor, or the GET/PUT routes' 405 from Starlette listing only one route's methods |
| `HEAD /api/v1/auth/me` | 405 after authentication | Starlette `Route` adds `HEAD` to every `GET` route |
| Unknown route, anonymous | 401 `unauthorized` (authentication runs first) | `{"detail": "Not Found"}` 404 |

On top of the individual answers, the reference runs its checks in three strict stages: (1) an
unreadable body or a value that cannot be converted stops everything and is reported at the JSON path of
the offending value together with the `request` entry; (2) only when the JSON converts cleanly are
non-nullable properties that are absent or `null` reported, all at once, under the model's own path; (3)
only when stage 2 is clean do the request's own rules run. Pydantic validates everything in one pass and
reports all errors together, so even with every message translated, the *set* of errors for a very
ordinary mistake (one field left out and another one too long) differs.

## Why the simple approaches fail

| Approach | What goes wrong |
|---|---|
| A custom `RequestValidationError` handler that translates Pydantic errors | The translation is lossy: Pydantic has already coerced `"30"` and `30.0`, merged absent and `null`, and run every validator; stage ordering cannot be recovered after the fact. Error `loc`s are field names, not the JSON names the client wrote (`FIRSTNAME`). |
| `ConfigDict(strict=True)` | Strict mode is per Python type, not per JSON token: it rejects ISO date strings in Python mode, accepts them in JSON mode but then also accepts integers for `datetime` (Unix timestamps), still treats absent and `null` alike for optional fields, cannot match property names case-insensitively, and still validates all fields in one pass. |
| `alias_generator` plus `populate_by_name` for case-insensitive names | Aliases are exact; only a pre-processing pass that rewrites the document can make `FIRSTNAME` match, and that pass needs the declared names anyway. |
| Field validators (`mode="before"`) | They see values after JSON parsing, cannot distinguish `3` from `3.0` from `3e0` once `json.loads` has produced floats, and cannot see whether the property was present. |
| Starlette's router as is | It matches templates with regular expressions in registration order, so a literal segment does not exclude an identifier template, `uuid` convertors turn non-GUIDs into 404 before "method not allowed" is considered, `Allow` lists only the first partially matching route's methods, and `Route` adds `HEAD` to `GET`. |

## The implementation

### 1. Pydantic declares the contract; a binder fills it

Request models remain Pydantic models (`RequestModel`), so the contract lives in one typed place: field
names, camelCase aliases, Python types, optionality and the request's own rules in `check()`:

```python
class RegisterRequest(RequestModel):
    email: str
    password: str
    first_name: str
    last_name: str
    phone: str | None = None

    def check(self, v: Validator, today: date) -> None:
        v.text("email", self.email).not_empty().max_length(254).email()
        ...
```

`api/binding.py` reads those declarations once per model (`fields_of`) and derives everything the
reference derives from its C# types: the JSON name, the conversion (`Kind.STR`, `INT`, `UUID`, `DATE`,
`DATETIME`, `TIME`, `ENUM`, `LIST`, `MODEL`), optionality, and whether the property is *required* in
stage 2. The last rule needs no hand-written tables: the reference's model validation requires exactly
the non-nullable *reference* types, which in the Python declarations are the non-optional `str`, `list`
and nested-model fields.

```python
required = spec.kind in (Kind.STR, Kind.LIST, Kind.MODEL) and not spec.optional
specs.append(FieldSpec(name, alias, _simple_fold(alias), spec, _upper_first(alias) if required else None))
```

The model is then filled with `model_construct`, i.e. without running Pydantic's validation at all.

### 2. Parsing that keeps what the reference sees

```python
def parse_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=JsonObject, parse_float=FloatLiteral,
                          parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _BindFailureError(_PathFinder(text).failure_path()) from None
```

`object_pairs_hook` keeps every member in document order (duplicates included, so the last one can win),
`parse_float` keeps non-integer numbers as their literal text so `30.0` and `3e1` can be rejected for
`int` while `30` is accepted, and `NaN`/`Infinity` are refused. Valid documents therefore cost one C-level
`json.loads`. Only for a malformed document does a small recursive reader (`_PathFinder`) re-read the text
to find the path at which it fails (`email` for `{"email": `, `[1]` for `[1,]`), because the standard
library reports only a character offset.

### 3. Stage 1, conversion with the reference's rules

`_convert` applies explicit rules per kind: UUIDs only in the 36-character form, dates only as
`YYYY-MM-DD`, instants as ISO 8601 without surrounding spaces (no offset means UTC), integers only as
32-bit integer literals (`type(value) is int`, so `true` is not 1), booleans only as JSON booleans,
enumerations by snake_case or PascalCase name, `null` accepted only for optional fields and lists, and the
time converter's own message for strings and `null`. Members are found case-insensitively with .NET's
simple case mapping (character by character), and the path uses the name *as the client wrote it*. The
first failure stops binding and becomes `{path: ["The value is missing or not valid."], "request": [...]}`.

### 4. Stage 2 and stage 3

```python
missing = FieldErrors()
_missing_required(document, model, "", missing)      # absent or null, also inside list elements
if len(missing):
    raise validation(missing)                         # "The Email field is required.", "specialties[0].Name"
bound.validate_rules(today)                           # the reference's FluentValidation messages
```

Stage 2 uses the model's PascalCase property names for nested list elements
(`specialties[0].Name`), exactly as the reference's model-state keys look. Stage 3 runs the request's
`check()` through a small validator with the reference's message templates and display names (lengths in
UTF-16 code units like .NET strings).

### 5. Routing with the reference's tree semantics

FastAPI still routes requests (each operation is an `APIRoute`, which unlike Starlette's `Route` does
not add `HEAD`), but every handler and the router's own 404/405 exception path go through
`Router.resolve`, which reproduces the reference's routing tree:

```python
segments = [segment.lower() for segment in path.strip("/").split("/")]
remaining = [r for r in self.routes if len(r.segments) == len(segments)]
for index, segment in enumerate(segments):
    literal = [r for r in remaining if r.segments[index] == segment]
    remaining = literal or [r for r in remaining if r.segments[index] is None]
```

A literal segment of a template wins over identifier segments (identifiers are GUID-constrained, so a
literal like `self-registration` can never be one); any other value follows the identifier branch
without checking the constraint. "Method not allowed" is decided on those candidates and `Allow` lists
their methods in ordinal order; only then is the GUID constraint checked, which turns a match into an
empty 404. Both 404 and 405 are answered only after the authentication pipeline, so an anonymous probe
of an unknown route still gets 401. The outer ASGI middleware lower-cases the path for routing and drops a
trailing slash, because the reference's routing does both.

### 6. Writing answers the same way

Problems are written with the reference's member order (`type, title, status, code, detail, traceId,
errors`) and header rules (`WWW-Authenticate` only for the authentication challenge, `Retry-After` for
the rate limiter, `Expires: -1`/`Pragma: no-cache` for everything the reference's exception handler
produces, the framework-style document for 415). All JSON goes through `reference_json`, which takes the
standard library's compact output and rewrites escapes with one regular expression so that `" & ' + < >`
and non-ASCII characters become upper-case `\uXXXX` like the reference serializer, while escaped
backslashes are left alone.

## How it is proven

| Evidence | What it shows |
|---|---|
| Black-box comparison against the reference (release build of the reference, own database each; 223 exchanges: anonymous and routing edge cases, 22 malformed or unusual bodies, CORS, a full practice set-up, booking, replay, key reuse, conflicts, life cycle, portal patient, invitations, MFA, refresh replay, query errors) | 222 exchanges identical in status, selected headers, normalized body and member order. Two differences found during the work were fixed (whitespace-only bodies; `GET /patients/self-registration` → 405 `Allow: POST`). The remaining difference is a reference defect (`specialties: [null]` → 500 there, 400 here; [troubleshooting.md](troubleshooting.md#4-defect-in-the-reference-implementation-not-reproduced)). |
| `tests/unit/test_binding.py` | Each conversion rule, stage-1 paths (including list elements and malformed documents), stage-2 keys for top-level and nested properties, stage ordering, null handling per kind, duplicate and case-insensitive names, the byte-order mark, the media-type check and the size limit. |
| `tests/unit/test_infrastructure.py` | Query binding messages (`for Page` only for paging), GUID forms, path normalization, routing-tree resolution (literal precedence, 405 before the GUID constraint, ordinal `Allow`). |
| `tests/integration/test_http_behaviour.py`, `test_auth.py`, `test_providers_patients.py`, `test_contract.py` | The documents end to end over HTTP, including 413/415, empty 404/405 with `Allow`, problem headers, CORS, and that real responses validate against the OpenAPI schemas. |
| `tests/unit/test_scheduling.py` | The canonical booking request is byte-identical to the reference serializer's, proven by recomputing a request hash that the reference API stored in its database. |

## Trade-offs

- **Two type systems for one request.** The declarations are Pydantic models, but Pydantic never
  validates client input. This keeps one source of truth for the contract and lets responses use
  Pydantic fully, at the cost of a binder (about 490 lines including docstrings) that must learn any new field type before a
  model can use it (it raises `TypeError` at import time for an unsupported annotation, so the gap cannot
  go unnoticed).
- **Performance.** Valid bodies are parsed once by the C parser and converted field by field in Python;
  in the load tests booking requests took 11 ms at the median end to end (15 ms under the
  stress profile), dominated by the database round trips of the booking transaction. The slow path finder runs only for malformed JSON.
- **Emulating framework details.** Some reproduced details (the 415 `traceId` format, `Allow`
  ordering, the routing tree's literal precedence) are framework artefacts rather than design
  decisions. They are isolated in `api/binding.py`, `api/routing.py`, `api/responses.py` and
  `api/pipeline.py`, so they can be dropped together if the contract ever specifies them differently.
