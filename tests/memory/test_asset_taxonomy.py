"""Classification uses resolved resources, including containment and write boundaries."""

from types import SimpleNamespace

from nanobot.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from nanobot.memory.asset_taxonomy import error_class, operation_family, resource_class


def test_resolved_workspace_resources_are_categories_not_hashes(tmp_path):
    read=ReadFileTool(workspace=tmp_path,allowed_dir=tmp_path)
    for name in ("USER.md","SOUL.md","README.txt"):
        (tmp_path/name).write_text("fixture")
        assert resource_class("read_file",read,{"path":name}) == "workspace-doc"
    (tmp_path/"no-extension").write_text("not a directory")
    assert resource_class("read_file",read,{"path":"no-extension"}) == "unknown"
    assert resource_class("read_file",read,{"resource_key":"sha256:README.md"}) == "unknown"
    assert resource_class("read_file",None,{"resource_key":"sha256:README.md"}) == "unknown"
    (tmp_path/"folder").mkdir()
    assert resource_class("list_dir",ListDirTool(workspace=tmp_path),{"path":"folder"}) == "workspace-dir"
    assert resource_class("write_file",WriteFileTool(workspace=tmp_path,allowed_dir=tmp_path),{"path":"new.md"}) == "workspace-doc"


def test_unresolved_outside_and_symlink_targets_are_not_workspace_documents(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    outside=tmp_path/"outside.md"
    outside.write_text("outside")
    (project/"link.md").symlink_to(outside)
    read=ReadFileTool(workspace=project,allowed_dir=project)
    for path in (str(outside),"../outside.md","link.md"):
        assert resource_class("read_file",read,{"path":path}) == "unknown"


def test_classification_does_not_parse_shell_or_hashes():
    assert resource_class("exec",None,{"command":"cat USER.md"}) == "unknown"
    assert resource_class("read_file",SimpleNamespace(_workspace="."),{"path":"sha256:resource.md"}) == "unknown"
    assert resource_class("web_fetch",None,{"url":"https://example.com/page"}) == "external-url"
    assert resource_class("web_fetch",None,{"url":"file:///tmp/example"}) == "unknown"
    assert error_class("unknown") is None and error_class(None) is None
    assert error_class("web_search_timeout") == "timeout"
    assert operation_family("write_file") == "controlled-change"
    assert operation_family("exec") == "process-execute"
