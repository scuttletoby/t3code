// Fork: the long press on a file link that has no primary action (the button chip). Its click
// handler opens the menu, so the click that ends a long press must not open it a second time.
import { expect, it, vi } from "vite-plus/test";

import { act, fakeTimers, render } from "../../../test/render";

const state = vi.hoisted(() => ({ choose: vi.fn() }));

vi.mock(import("../../localApi"), async (original) => ({
  ...(await original()),
  readLocalApi: () => ({ contextMenu: { show: state.choose } }) as never,
}));

import { MarkdownFileLink } from "./MarkdownFileLink";

it("opens the menu once for a long press on a file link with no primary action", async () => {
  state.choose.mockReset().mockResolvedValue(null);
  await using screen = await render(
    <MarkdownFileLink
      environmentId={null}
      href="readme.md"
      targetPath="readme.md"
      iconPath="readme.md"
      workspaceRelativePath="readme.md"
      panelPath={null}
      label="Readme"
      copyMarkdown="[Readme](readme.md)"
      theme="dark"
      onOpenInPanel={vi.fn()}
      resolveDragPath={async () => "/work/readme.md"}
      resolveCopyPaths={async () => ({
        relativePath: "readme.md",
        fullPath: "/work/readme.md",
        pathOnly: "/work/readme.md",
      })}
      openInEditorMenuLabel="Open in editor"
    />,
  );
  const button = screen.container.querySelector("button")!;
  const touch = (type: string) =>
    button.dispatchEvent(
      new PointerEvent(type, {
        bubbles: true,
        cancelable: true,
        pointerType: "touch",
        clientX: 5,
        clientY: 5,
      }),
    );
  // The click that ends a touch press has detail 1; a keyboard activation has detail 0.
  const click = () =>
    act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 }));
    });
  {
    using _timers = fakeTimers();
    await act(async () => {
      touch("pointerdown");
      vi.advanceTimersByTime(500);
      touch("pointerup");
    });
  }
  expect(state.choose).toHaveBeenCalledOnce();
  await click();
  expect(state.choose).toHaveBeenCalledOnce();
  // Only that one click is taken: the next tap opens the menu as usual.
  await click();
  expect(state.choose).toHaveBeenCalledTimes(2);
});
