/* File bytes stay local to this application; never parse HTML in the browser. */
async function readInputFile(file) {
  if (!/\.(txt|md|html?)$/i.test(file.name)) throw Error(`${file.name}：仅支持 TXT、Markdown 和 HTML。`);
  if (file.size > 450000) throw Error(`${file.name}：文件超过当前 450 KB 导入上限，未截取内容。`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  const encoding =
    bytes[0] === 0xff && bytes[1] === 0xfe
      ? 'utf-16le'
      : bytes[0] === 0xfe && bytes[1] === 0xff
        ? 'utf-16be'
        : 'utf-8';
  let content;
  try {
    content = new TextDecoder(encoding, { fatal: true }).decode(bytes);
  } catch {
    throw Error(`${file.name}：编码无法可靠识别，请转为 UTF-8 或带 BOM 的 UTF-16，未用乱码继续分析。`);
  }
  if ([...content].length > 100000) throw Error(`${file.name}：超过当前单项 100,000 字符限制，未截取内容。`);
  if (!content.trim()) throw Error(`${file.name}：没有可分析的文字。`);
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return { name: file.name, content, imported_file: { name: file.name, data_base64: btoa(binary) } };
}
