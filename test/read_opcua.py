from asyncua.sync import Client

# Update to your actual Kepware endpoint (local or remote)
OPCUA_URL = "opc.tcp://localhost:49320"

# Try these first; different Kepware setups often use different namespace indexes.
TAG_CANDIDATES = {
    "Ramp1": [
        "ns=2;s=Channel1.Simulation Examples.Functions.Ramp1",
        "ns=3;s=Channel1.Simulation Examples.Functions.Ramp1",
    ],
    "Sine1": [
        "ns=2;s=Channel1.Simulation Examples.Functions.Sine1",
        "ns=3;s=Channel1.Simulation Examples.Functions.Sine1",
    ],
    "Square1": [
        "ns=2;s=Channel1.Simulation Examples.Functions.Square1",
        "ns=3;s=Channel1.Simulation Examples.Functions.Square1",
    ],
}


def try_read_value(client: Client, node_id: str):
    node = client.get_node(node_id)
    return node.read_value()


def discover_tags_by_name(client: Client, target_names: set[str], max_depth: int = 6, max_nodes: int = 5000):
    results = {}
    stack = [(client.nodes.objects, 0)]
    visited = 0

    while stack and visited < max_nodes and len(results) < len(target_names):
        node, depth = stack.pop()
        visited += 1

        try:
            browse_name = node.read_browse_name().Name
        except Exception:
            browse_name = None

        if browse_name in target_names and browse_name not in results:
            results[browse_name] = node

        if depth >= max_depth:
            continue

        try:
            children = node.get_children()
        except Exception:
            children = []

        for child in children:
            stack.append((child, depth + 1))

    return results


def main() -> None:
    client = Client(url=OPCUA_URL)
    client.connect()
    print(f"Connected to OPC UA Server: {OPCUA_URL}")

    try:
        unresolved = set()

        for tag_name, node_ids in TAG_CANDIDATES.items():
            read_success = False

            for node_id in node_ids:
                try:
                    value = try_read_value(client, node_id)
                    print(f"{tag_name} Value: {value} (nodeid={node_id})")
                    read_success = True
                    break
                except Exception as exc:
                    print(f"Error reading {tag_name} ({node_id}): {exc}")

            if not read_success:
                unresolved.add(tag_name)

        if unresolved:
            print(f"Trying discovery for unresolved tags: {sorted(unresolved)}")
            discovered = discover_tags_by_name(client, unresolved)

            for tag_name in sorted(unresolved):
                node = discovered.get(tag_name)
                if node is None:
                    print(f"{tag_name}: not found during browse discovery.")
                    continue
                try:
                    value = node.read_value()
                    print(f"{tag_name} Value: {value} (discovered nodeid={node.nodeid.to_string()})")
                except Exception as exc:
                    print(f"{tag_name}: found node {node.nodeid.to_string()} but read failed: {exc}")
    finally:
        client.disconnect()
        print("Disconnected from OPC UA Server.")


if __name__ == "__main__":
    main()
