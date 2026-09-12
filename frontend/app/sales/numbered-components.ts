const NUMBERED_COMPONENT_LINE = /^\s*(?:需求\s*)?(?:[（(]\s*)?(\d{1,3})(?:\s*[)）])?\s*[、,，.．。:：;；\-—]\s*(\S.*)$/;

export function validateNumberedComponentLines(value: string): string | null {
  const lines = value
    .split("\n")
    .map((line, index) => ({ lineNumber: index + 1, text: line.trim() }))
    .filter((line) => line.text.length > 0);

  if (lines.length === 0) return "请填写客户需求。";
  if (lines.length > 200) return "一次最多提交 200 个组件。";

  for (let index = 0; index < lines.length; index += 1) {
    const { lineNumber, text } = lines[index];
    const expected = index + 1;
    const match = text.match(NUMBERED_COMPONENT_LINE);
    if (!match) {
      return `第 ${lineNumber} 行必须以连续序号 ${expected}. 开头，并且一行只写一个组件。`;
    }
    const actual = Number(match[1]);
    if (actual !== expected) {
      return `第 ${lineNumber} 行序号应为 ${expected}，当前为 ${actual}。`;
    }
  }
  return null;
}
