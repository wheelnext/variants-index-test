# variant-index

The index supports variant metadata that validates against
[`src/schema.json`](src/schema.json), using `jsonschema-rs`. Downloaded and
cached metadata are validated against this local schema; the metadata's
`$schema` URL is neither fetched nor rewritten. Metadata is never upgraded or
converted.

Invalid JSON or incompatible metadata causes that release and its wheels to be
ignored. Packages in `[[index]]` without a compatible variant wheel release are
logged as ignored and omitted from the top-level index. Ordinary PyPI wheels
remain available for included packages, except for releases whose metadata was
rejected.

Provider packages use `[[provider-index]]` with the same fields as `[[index]]`:

```toml
[[provider-index]]
name = "quansight"
maintainers = [
    { name = "Michał Górny", email = "mgorny@quansight.com" },
    { name = "Ralf Gommers", email = "rgommers@quansight.com" },
]
registry = "https://pypi.anaconda.org/mgorny/simple/"
packages = [
    "blas-lapack-variant-provider",
    "openmp-variant-provider",
    "provider-variant-aarch64",
    "provider-variant-x86-64",
]
```

Provider indexes list ordinary `.whl` files and entirely skip variant metadata
downloads, cache reads, and validation. Wheels with variant labels are logged as
warnings and skipped. The configured registry takes priority, with PyPI
supplying additional versions as for variant packages. Providers with at least
one ordinary wheel appear in the top-level index; those with no eligible wheels
are ignored. Both section types are optional, and each package can belong to
only one index across both types.

```bash
uv venv
uv sync --dev --all-extras --frozen

make clean
make build
make serve
```

Open http://127.0.0.1:8000 to view the generated index. Use
`make serve PORT=8080` to choose another port, and press Ctrl+C to stop the
server.

Run the regression tests without network access to package registries:

```bash
make test
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```
