(function () {
  const MENU_ITEMS = [
    ["bold", "Жирный"],
    ["italic", "Курсив"],
    ["underline", "Подчеркнутый"],
    ["strikeThrough", "Зачеркнутый"],
    ["spoiler", "Спойлер"],
    ["link", "Ссылка"],
    ["unlink", "Убрать ссылку"],
    ["code", "Код"],
    ["pre", "Блок кода"],
    ["quote", "Цитата"],
    ["quote-expandable", "Раскрываемая цитата"],
    ["clear", "Очистить стиль"],
  ];

  function closestBuilder(target) {
    return target ? target.closest("[data-caption-builder]") : null;
  }

  function updateBuilder(builder) {
    if (!builder) return;
    const editor = builder.querySelector("[data-caption-editor]");
    const preview = builder.querySelector("[data-caption-preview]");
    const input = builder.querySelector("[data-caption-html]");
    if (!editor || !preview || !input) return;
    const html = editor.innerHTML.trim();
    input.value = html;
    preview.innerHTML = html || '<span class="muted-preview">Подпись пустая</span>';
  }

  function selectionInEditor(editor) {
    const selection = window.getSelection();
    if (!selection || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return null;
    return range.cloneRange();
  }

  function restoreSelection(builder) {
    const range = builder._captionRange;
    if (!range) return;
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function rangeTouchesTag(range, tagName) {
    const start = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer
      : range.startContainer.parentElement;
    const end = range.endContainer.nodeType === Node.ELEMENT_NODE
      ? range.endContainer
      : range.endContainer.parentElement;
    if (start?.closest(tagName) || end?.closest(tagName)) return true;
    const fragment = range.cloneContents();
    return Boolean(fragment.querySelector?.(tagName));
  }

  function currentEditableBlock(range, editor) {
    const node = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer
      : range.startContainer.parentElement;
    if (!node || node === editor) return null;
    return node.closest("blockquote, pre, p, div, tg-spoiler, code");
  }

  function isEmptyBlock(block) {
    if (!block) return false;
    const clone = block.cloneNode(true);
    clone.querySelectorAll("br").forEach((br) => br.remove());
    return clone.textContent.trim() === "";
  }

  function replaceEmptyBlock(builder, block, element) {
    block.replaceWith(element);
    const range = document.createRange();
    if (!element.textContent.trim()) {
      element.appendChild(document.createElement("br"));
      range.setStart(element, 0);
    } else {
      range.selectNodeContents(element);
    }
    range.collapse(false);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    builder._captionRange = range.cloneRange();
  }

  function insertPlainLine(builder) {
    const editor = builder.querySelector("[data-caption-editor]");
    editor.focus();
    restoreSelection(builder);
    const range = selectionInEditor(editor);
    if (!range) return;
    const paragraph = document.createElement("p");
    paragraph.appendChild(document.createElement("br"));
    const block = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer
      : range.startContainer.parentElement;
    const currentBlock = block?.closest("blockquote, pre, p, div, tg-spoiler, code, b, strong, i, em, u, s, strike, del, a");
    if (currentBlock && currentBlock !== editor) {
      currentBlock.after(paragraph);
    } else {
      range.insertNode(paragraph);
    }
    const nextRange = document.createRange();
    nextRange.setStart(paragraph, 0);
    nextRange.collapse(true);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(nextRange);
    builder._captionRange = nextRange.cloneRange();
    updateBuilder(builder);
  }

  function wrapSelection(builder, tagName, attrs = {}) {
    const editor = builder.querySelector("[data-caption-editor]");
    editor.focus();
    restoreSelection(builder);
    const range = selectionInEditor(editor);
    if (tagName === "blockquote" && range && rangeTouchesTag(range, "blockquote")) {
      return;
    }
    const element = document.createElement(tagName);
    Object.entries(attrs).forEach(([name, value]) => {
      if (value === true) element.setAttribute(name, "");
      if (typeof value === "string") element.setAttribute(name, value);
    });
    if (!range || range.collapsed) {
      if (tagName === "blockquote") {
        const active = window.getSelection()?.anchorNode;
        const activeElement = active?.nodeType === Node.ELEMENT_NODE ? active : active?.parentElement;
        if (activeElement?.closest("blockquote")) return;
      }
      const block = range ? currentEditableBlock(range, editor) : null;
      if (block && isEmptyBlock(block)) {
        replaceEmptyBlock(builder, block, element);
        return;
      }
      element.textContent = tagName === "blockquote" ? "Текст цитаты" : "Текст";
      editor.appendChild(element);
      editor.appendChild(document.createElement("br"));
      return;
    }
    element.appendChild(range.extractContents());
    range.insertNode(element);
    const selection = window.getSelection();
    selection.removeAllRanges();
    const nextRange = document.createRange();
    nextRange.selectNodeContents(element);
    selection.addRange(nextRange);
    builder._captionRange = nextRange.cloneRange();
  }

  function applyCommand(builder, command) {
    const editor = builder.querySelector("[data-caption-editor]");
    editor.focus();
    restoreSelection(builder);
    if (command === "link") {
      const href = window.prompt("Ссылка");
      if (href) document.execCommand("createLink", false, href);
    } else if (command === "unlink") {
      document.execCommand("unlink", false);
    } else if (command === "spoiler") {
      wrapSelection(builder, "tg-spoiler");
    } else if (command === "code") {
      wrapSelection(builder, "code");
    } else if (command === "pre") {
      wrapSelection(builder, "pre");
    } else if (command === "quote") {
      wrapSelection(builder, "blockquote");
    } else if (command === "quote-expandable") {
      wrapSelection(builder, "blockquote", { expandable: true });
    } else if (command === "clear") {
      document.execCommand("removeFormat", false);
    } else {
      document.execCommand(command, false);
    }
    updateBuilder(builder);
    hideMenu();
  }

  function menu() {
    let element = document.querySelector("[data-caption-context-menu]");
    if (element) return element;
    element = document.createElement("div");
    element.className = "caption-context-menu";
    element.dataset.captionContextMenu = "1";
    element.hidden = true;
    element.innerHTML = MENU_ITEMS.map(([command, label]) => (
      `<button type="button" data-caption-menu-command="${command}">${label}</button>`
    )).join("");
    document.body.appendChild(element);
    return element;
  }

  function showMenu(builder, x, y) {
    const element = menu();
    element._captionBuilder = builder;
    element.hidden = false;
    const maxLeft = Math.max(12, window.innerWidth - 230);
    const maxTop = Math.max(12, window.innerHeight - 330);
    element.style.left = `${Math.min(x, maxLeft)}px`;
    element.style.top = `${Math.min(y, maxTop)}px`;
  }

  function hideMenu() {
    const element = document.querySelector("[data-caption-context-menu]");
    if (!element) return;
    element.hidden = true;
    element._captionBuilder = null;
  }

  function initCaptionBuilders(root = document) {
    root.querySelectorAll("[data-caption-builder]").forEach((builder) => {
      if (builder.dataset.captionReady === "1") {
        updateBuilder(builder);
        return;
      }
      builder.dataset.captionReady = "1";
      const editor = builder.querySelector("[data-caption-editor]");
      editor.addEventListener("input", () => updateBuilder(builder));
      editor.addEventListener("keyup", () => {
        const range = selectionInEditor(editor);
        if (range) builder._captionRange = range;
      });
      editor.addEventListener("keydown", (event) => {
        if (event.shiftKey && event.key === "Enter") {
          event.preventDefault();
          insertPlainLine(builder);
          return;
        }
        if (!(event.ctrlKey && event.code === "Space")) return;
        const range = selectionInEditor(editor);
        if (range) builder._captionRange = range;
        const rect = editor.getBoundingClientRect();
        event.preventDefault();
        showMenu(builder, rect.left + 18, rect.top + 18);
      });
      editor.addEventListener("mouseup", () => {
        const range = selectionInEditor(editor);
        if (range) builder._captionRange = range;
      });
      updateBuilder(builder);
    });
  }

  if (!window.captionBuilderListenersReady) {
    window.captionBuilderListenersReady = true;
    document.addEventListener("contextmenu", (event) => {
      const editor = event.target.closest("[data-caption-editor]");
      if (!editor) return;
      const builder = closestBuilder(editor);
      if (!builder) return;
      const range = selectionInEditor(editor);
      if (range) builder._captionRange = range;
      event.preventDefault();
      event.stopPropagation();
      showMenu(builder, event.clientX, event.clientY);
    }, true);
    document.addEventListener("click", (event) => {
      const menuButton = event.target.closest("[data-caption-menu-command]");
      if (menuButton) {
        const owner = closestBuilder(menuButton) || menuButton.closest("[data-caption-context-menu]")?._captionBuilder;
        if (owner) applyCommand(owner, menuButton.dataset.captionMenuCommand);
        return;
      }
      if (!event.target.closest("[data-caption-context-menu]")) hideMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hideMenu();
    });
    document.addEventListener("submit", (event) => {
      event.target.querySelectorAll("[data-caption-builder]").forEach(updateBuilder);
    }, true);
    window.addEventListener("scroll", hideMenu, true);
    window.addEventListener("resize", hideMenu);
  }

  window.initCaptionBuilders = initCaptionBuilders;
  initCaptionBuilders(document);
})();
