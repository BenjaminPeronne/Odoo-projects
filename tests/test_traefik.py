import unittest

from odoo_manager_core.traefik import (
    PublishedPort,
    TraefikInstance,
    compose_service_container_name,
    compose_service_ports,
    detect_traefik_instances,
    entrypoints_from_arguments,
    entrypoints_from_config,
    entrypoints_from_environment,
    parse_docker_ports,
    reset_traefik_entrypoint_cache,
    same_directory,
    select_traefik_instance,
    url_with_port,
)


MIDDLEWARE_LABELS = (
    "traefik.http.middlewares.odoo-forward.headers.customrequestheaders.X-Forwarded-Proto=http,"
    "traefik.http.middlewares.odoo-compress.compress=true,"
    "traefik.http.middlewares.odoo-headers.headers.hostsproxyheaders=websocket,Upgrade"
)


def instance(**values):
    defaults = {
        "container_id": "abc",
        "name": "traefik",
        "image": "traefik:3.6",
        "state": "running",
        "published": (PublishedPort("127.0.0.1", 80, 80),),
        "networks": ("traefik-local",),
        "labels": MIDDLEWARE_LABELS,
        "entrypoints": {"web": 80},
    }
    defaults.update(values)
    return TraefikInstance(**defaults)


class TraefikParsingTests(unittest.TestCase):
    def test_docker_ports_keep_custom_host_ports_and_ranges(self):
        ports = parse_docker_ports("127.0.0.1:8080->80/tcp, :::8080->80/tcp, 443/tcp, 0.0.0.0:9000-9001->9000-9001/tcp")

        self.assertIn(PublishedPort("127.0.0.1", 8080, 80), ports)
        self.assertIn(PublishedPort("::", 8080, 80), ports)
        self.assertIn(PublishedPort("0.0.0.0", 9001, 9001), ports)
        self.assertFalse(any(port.container_port == 443 for port in ports))

    def test_compose_ports_are_read_for_the_traefik_service_only(self):
        compose = (
            "services:\n"
            "  whoami:\n"
            "    ports:\n"
            "      - 81:80\n"
            "  traefik:\n"
            "    container_name: my-proxy\n"
            "    ports:\n"
            '      - "8080:80"   # port 80 occupé par Apache\n'
            "      - 127.0.0.1:8443:443\n"
            "    networks:\n"
            "      - traefik-local\n"
        )

        self.assertEqual(
            [PublishedPort("", 8080, 80), PublishedPort("127.0.0.1", 8443, 443)],
            compose_service_ports(compose),
        )
        self.assertEqual("my-proxy", compose_service_container_name(compose))

    def test_compose_ports_with_variables_or_long_syntax_are_not_rewritten(self):
        variables = "services:\n  traefik:\n    ports:\n      - ${HTTP_PORT:-80}:80\n"
        long_syntax = "services:\n  traefik:\n    ports:\n      - target: 80\n        published: 8080\n"

        self.assertIsNone(compose_service_ports(variables))
        self.assertIsNone(compose_service_ports(long_syntax))
        self.assertIsNone(compose_service_ports("services:\n  proxy:\n    image: traefik\n"))
        self.assertEqual([], compose_service_ports("services:\n  traefik:\n    image: traefik\n"))

    def test_entrypoints_are_read_from_yaml_toml_arguments_and_environment(self):
        yaml = 'entryPoints:\n  web:\n    address: ":8000"\n  websecure:\n    address: ":8443"\napi:\n  dashboard: true\n'
        toml = '[entryPoints]\n  [entryPoints.http]\n    address = ":8081"\n'

        self.assertEqual({"web": 8000, "websecure": 8443}, entrypoints_from_config(yaml))
        self.assertEqual({"http": 8081}, entrypoints_from_config(toml))
        self.assertIsNone(entrypoints_from_config("api:\n  dashboard: true\n"))
        self.assertEqual(
            {"web": 80, "metrics": 9100},
            entrypoints_from_arguments(["--entrypoints.web.address=:80", "--entryPoints.metrics.address=:9100", "--dns.address=:53/udp"]),
        )
        self.assertEqual({"web": 8888}, entrypoints_from_environment(["TRAEFIK_ENTRYPOINTS_WEB_ADDRESS=:8888", "TZ=UTC"]))

    def test_http_port_follows_the_web_entrypoint_mapping(self):
        custom = instance(entrypoints={"web": 8000, "traefik": 8080}, published=(
            PublishedPort("0.0.0.0", 8080, 8080),
            PublishedPort("0.0.0.0", 9000, 8000),
        ))

        self.assertEqual(9000, custom.http_port)
        self.assertEqual(8000, instance(networks=("host",), published=(), entrypoints={"web": 8000}).http_port)
        self.assertIsNone(instance(published=()).http_port)

    def test_incompatible_instances_explain_why(self):
        other = instance(entrypoints={"http": 80}, networks=("proxy",), labels="")

        problems = other.compatibility_problems()

        self.assertEqual(2, len(problems))
        self.assertIn("aucun entrypoint « web »", problems[0])
        self.assertIn("réseau traefik-local", problems[1])
        self.assertEqual(("odoo-forward", "odoo-compress", "odoo-headers"), other.missing_middlewares)
        self.assertEqual([], instance().compatibility_problems())

    def test_selection_prefers_running_compatible_managed_instance(self):
        stopped = instance(container_id="1", state="exited", published=())
        foreign = instance(container_id="2", name="proxy", networks=("proxy",))
        managed = instance(container_id="3", working_dir="/tools/traefik")

        selected = select_traefik_instance([stopped, foreign, managed], lambda item: item.working_dir == "/tools/traefik")

        self.assertEqual("3", selected.container_id)
        self.assertIsNone(select_traefik_instance([], lambda _item: False))

    def test_compose_working_directory_matches_windows_and_wsl_paths(self):
        self.assertTrue(same_directory(r"C:\Users\Demo\docker-local-tools\traefik", "c:/users/demo/docker-local-tools/traefik"))
        self.assertTrue(same_directory("/mnt/c/Users/Demo/traefik", r"C:\Users\Demo\traefik", "/mnt/c/Users/Demo/traefik"))
        self.assertFalse(same_directory("", "/tools/traefik"))

    def test_url_gets_port_only_when_it_is_not_80(self):
        self.assertEqual("http://dev.demo.localhost/", url_with_port("http://dev.demo.localhost/", 80))
        self.assertEqual("http://dev.demo.localhost:8080/", url_with_port("http://dev.demo.localhost/", 8080))
        self.assertEqual("http://localhost:8069/", url_with_port("http://localhost:8069/", 8080))


class TraefikDetectionTests(unittest.TestCase):
    def setUp(self):
        reset_traefik_entrypoint_cache()

    def test_detection_reads_ports_networks_and_config_once_per_container_state(self):
        calls = []
        line = "\t".join((
            "id1", "edge", "docker.io/library/traefik:v2.11", "running", "0.0.0.0:8000->80/tcp",
            "traefik-local", "/home/demo/proxy", MIDDLEWARE_LABELS,
        ))
        unrelated = "\t".join(("id2", "odoo-DEMO", "sudokeys/docker-odoo-local:18.0", "running", "8069/tcp", "traefik-local", "", ""))

        def capture(command, timeout=10):
            calls.append(command)
            if command[1] == "ps":
                return 0, f"{line}\n{unrelated}"
            if command[1] == "inspect":
                return 0, '["traefik"]\t["PATH=/bin"]'
            if command[1] == "exec":
                return 0, 'entryPoints:\n  web:\n    address: ":80"\n'
            return 1, ""

        first = detect_traefik_instances(capture, ["docker"])
        second = detect_traefik_instances(capture, ["docker"])

        self.assertEqual(1, len(first))
        self.assertEqual("edge", first[0].name)
        self.assertEqual(8000, first[0].http_port)
        self.assertEqual("/home/demo/proxy", first[0].working_dir)
        self.assertEqual(first, second)
        self.assertEqual(1, sum(command[1] == "exec" for command in calls))

    def test_detection_reports_unknown_when_docker_does_not_answer(self):
        self.assertIsNone(detect_traefik_instances(lambda _command, timeout=10: (1, "Cannot connect"), ["docker"]))


if __name__ == "__main__":
    unittest.main()
