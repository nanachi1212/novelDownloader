from adapter_tools import domains_from_url, generate_adapter_template, safe_module_name


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
