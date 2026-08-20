// Pinned to 'en-US' rather than left locale-less: an unspecified locale falls back to
// the browser's own setting, so the same number would render with Western digits
// (3,733) on one device and Eastern Arabic-Indic digits (٣٬٧٣٣) on another - the owner
// seeing different numerals depending which phone they're on. .ltr-num (index.css)
// keeps the digit group direction stable; this keeps the digits themselves stable.
export function n(value: number): string {
  return value.toLocaleString('en-US')
}
