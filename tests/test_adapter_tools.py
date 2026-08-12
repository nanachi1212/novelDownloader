from adapter_tools import adapter_is_enabled, disable_adapter, domains_from_url, generate_adapter_template, safe_module_name


def test_adapter_generator_builds_generic_domain_adapter():
    code = generate_adapter_template("https://www.example.com/books/123/", "Example Site", "Example")
    assert "class ExampleSiteAdapter(GenericAdapter)" in code
    assert "domains = ['www.example.com', 'example.com']" in code
    assert "adapter_label = 'Example'" in code


def test_adapter_generator_sanitizes_module_names_and_domains():
    assert safe_module_name("123 My Site!") == "site_123_my_site"
    assert domains_from_url("https://foo.example:443/book/1") == [
        "foo.example",
        "www.foo.example",
    ]


def test_external_adapter_can_be_installed_disabled(tmp_path):
    path = tmp_path / "external.py"
    path.write_text("raise RuntimeError('must not run')", encoding="utf-8")
    disable_adapter(path)
    assert not adapter_is_enabled(path)
