/* 固定原页中的文字网格阅读；不解释数值适用条件，也不改识别文字。 */
(() => {
  const validBox = box => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
    && 0 <= box[0] && 0 <= box[1] && box[0] < box[2] && box[1] < box[3] && box[2] <= 1 && box[3] <= 1;
  const inside = (box, outer, dx = 0, dy = 0) => validBox(box)
    && outer[0] - dx <= box[0] && outer[1] - dy <= box[1] && outer[2] + dx >= box[2] && outer[3] + dy >= box[3];
  function valid(table, layout) {
    const grid = table.reading_grid;
    if (!grid || grid.status !== 'GEOMETRY_CANDIDATE' || !Number.isInteger(grid.row_count)
      || !Number.isInteger(grid.column_count) || grid.row_count < 1 || grid.row_count > 40
      || grid.column_count < 1 || grid.column_count > 40 || !Array.isArray(grid.cells)
      || !grid.cells.length || grid.cells.length > 200 || !Array.isArray(grid.unassigned_lines)) return false;
    const occupied = new Set(), indices = new Set(); let characters = 0;
    function validLine(line, cell) {
      if (!line || !Number.isInteger(line.line_index) || line.line_index < 0 || indices.has(line.line_index)
        || typeof line.text !== 'string' || !line.text.length) return false;
      indices.add(line.line_index); characters += line.text.length;
      return characters <= 16000 && (cell ? inside(line.box, cell.box, 2 / layout.width, 2 / layout.height)
        : line.box === null || validBox(line.box));
    }
    for (const cell of grid.cells) {
      if (!['row', 'column', 'row_span', 'column_span'].every(key => Number.isInteger(cell[key]) && cell[key] >= 1)
        || cell.row + cell.row_span - 1 > grid.row_count || cell.column + cell.column_span - 1 > grid.column_count
        || !inside(cell.box, table.box) || !Array.isArray(cell.lines)) return false;
      // 合并格占有多个位置，不能再叠入另一格或重复同一识别行。
      for (let r = cell.row; r < cell.row + cell.row_span; r++) {
        for (let c = cell.column; c < cell.column + cell.column_span; c++) {
          const key = `${r}:${c}`; if (occupied.has(key)) return false; occupied.add(key);
        }
      }
      if (!cell.lines.every(line => validLine(line, cell))) return false;
    }
    return grid.unassigned_lines.every(line => validLine(line, null));
  }
  // 只用当前已核页面的条文框选表；这表示阅读位置，不产生数值或适用性结论。
  function highlightedTable(layout) {
    const tables = layout?.tables;
    if (!Array.isArray(tables) || !tables.length || tables.some(table => !validBox(table.box))) return null;
    if (tables.length === 1) return tables[0];
    if (!Number.isFinite(layout.width) || !Number.isFinite(layout.height)
      || layout.width <= 0 || layout.height <= 0 || !Array.isArray(layout.highlights)) return null;
    const candidates = tables.filter(table => layout.highlights.some(line =>
      validBox(line.box) && inside(line.box, table.box, 2 / layout.width, 2 / layout.height)));
    // 多表都有定位或均无定位时不猜第一表，仍保留原有逐表选择入口。
    return candidates.length === 1 ? candidates[0] : null;
  }
  // 问答的行列证明只是候选。与新读取的固定页逐格相同，才允许自动选表及画框。
  function focus(proof, item, page, layout) {
    if (!proof || proof.status !== 'ORIGINAL_TABLE_READING' || proof.scope !== 'BASE_TABLE_ONLY'
      || proof.is_project_value !== false || !/^tr_[a-f0-9]{64}$/.test(proof.id || '')
      || proof.source_id !== item.source_id || proof.snapshot_id !== item.snapshot_id
      || proof.chunk_id !== item.id || proof.pdf_page !== page
      || layout.source_id !== item.source_id || layout.snapshot_id !== item.snapshot_id
      || layout.chunk_id !== item.id || layout.pdf_page !== page
      || !Array.isArray(proof.header_cells) || proof.header_cells.length !== 2) return null;
    const tables = layout.tables.filter(table => table.id === proof.table_id);
    if (tables.length !== 1 || !valid(tables[0], layout)) return null;
    const table = tables[0], grid = table.reading_grid;
    const compact = text => typeof text === 'string' ? text.replace(/\s/g, '') : '';
    const readable = lines => Array.isArray(lines) && lines.length > 0 && lines.every(line => line && validBox(line.box)
      && (line.origin === 'TEXT' || line.origin === 'OCR' && Number.isFinite(line.score) && line.score >= .9 && line.score <= 1));
    const equalBox = (a, b) => validBox(a) && validBox(b) && a.every((value, index) => value === b[index]);
    const equalCell = (a, b) => a && b && ['row','column','row_span','column_span'].every(key => a[key] === b[key])
      && equalBox(a.box,b.box) && Array.isArray(a.lines) && a.lines.length === b.lines.length
      && a.lines.every((line,index) => ['line_index','text','origin','score'].every(key => line[key] === b.lines[index][key])
        && equalBox(line.box,b.lines[index].box));
    const cells = [proof.row_cell,...proof.header_cells,proof.value_cell].map(candidate => {
      const found = grid.cells.filter(cell => equalCell(candidate,cell));
      return found.length === 1 && readable(found[0].lines) ? found[0] : null;
    });
    if (cells.some(cell => !cell)) return null;
    const [room,parent,header,value] = cells, text = cell => cell.lines.map(line => line.text.trim()).join('');
    const garage=proof.reading_kind==='GARAGE_AREA_BASELINE' && proof.unit==='m²'
      && /汽车库防火分区.*最大允许建筑面积\(m[²2]\)/.test(compact(proof.caption));
    // 车库表为同一表头行、等级行与类型列；采光表继续核对两层合并表头。
    const headersValid=garage ? text(parent)==='耐火等级' && parent.row===header.row
      && parent.column===room.column && parent.column_span===1 && header.column_span===1
      && parent.column!==header.column && parent.row+parent.row_span<=room.row && header.row+header.row_span<=room.row
      && /^[一二三四](?:、[一二三四])*级$/.test(text(room))
      && text(header).split(/[、,，]/).every(label=>['单层汽车库','多层汽车库','半地下汽车库','地下汽车库','高层汽车库'].includes(label))
      : parent.row+parent.row_span<=header.row && header.row+header.row_span<=room.row
        && header.column_span===1 && parent.column<=header.column
        && header.column+header.column_span<=parent.column+parent.column_span
        && /^(侧面|顶部)采光$/.test(text(parent)) && /采光系数.*标准值.*\(%\)/.test(text(header));
    if (room.row_span !== 1 || value.row !== room.row || value.column !== header.column
      || value.row_span !== 1 || value.column_span !== 1 || value.lines.length !== 1
      || !headersValid
      || !/^\d+(?:\.\d+)?$/.test(text(value)) || text(value) !== proof.value
      || text(room) !== proof.row_label || [text(parent),text(header)].join(' / ') !== proof.column_label) return null;
    const caption = proof.caption_line;
    const number = /^\s*表\s*(\d+(?:[.．]\d+)+)(?![\d.．])/.exec(proof.caption || '');
    const firstTop = Math.min(...grid.cells.map(cell => cell.box[1]));
    if (!/^\d+(?:\.\d+){2,3}$/.test(proof.table_number || '') || number?.[1].replace(/．/g,'.') !== proof.table_number
      || !readable([caption]) || compact(caption.text) !== compact(proof.caption)
      || firstTop - caption.box[3] < 0 || firstTop - caption.box[3] > .08
      || caption.box[0] < table.box[0] || caption.box[2] > table.box[2]
      || ![caption.text,...cells.flatMap(cell=>cell.lines.map(line=>line.text))]
        .every(value=>compact(item.text).includes(compact(value)))) return null;
    // 若新布局仍留有跨格文字，不能用旧证明忽略它。框只取新布局，不取传入坐标。
    if (grid.unassigned_lines.some(line => !line.box || cells.some(cell => line.box[0] < cell.box[2]
      && line.box[2] > cell.box[0] && line.box[1] < cell.box[3] && line.box[3] > cell.box[1]))) return null;
    return {table_id:table.id, marks:cells.map((cell,index)=>({cell,
      role:index===0?'ROW':index===3?'VALUE':'HEADER',label:index===0?(garage?'耐火等级':'房间'):index===3?`读数 ${proof.value}`:'表头'}))};
  }
  function render(container, table, selection = null, actions = null) {
    const grid = table.reading_grid, title = document.createElement('p'); title.className = 'reader-grid-note';
    title.textContent = '按原页网格排列识别文字，数字和表头请对照原图。';
    // 原表上方唯一的明确表题直接呈现，不能把表题中的单位藏进折叠记录。
    // 无位置、无表号或多份相似表题时仍保留原样，不补写标题或猜单位。
    const firstRowTop = Math.min(...grid.cells.map(cell => cell.box[1]));
    const captions = grid.unassigned_lines.filter(line => line.box && line.box[3] < firstRowTop
      && /^\s*表\s*\d+(?:[.．]\d+)+/.test(line.text));
    const captionLine = captions.length === 1 ? captions[0] : null;
    const caption = document.createElement('p'); caption.className = 'reader-grid-caption';
    if (captionLine) caption.textContent = captionLine.text;
    const scroll = document.createElement('div'); scroll.className = 'reader-grid-scroll'; scroll.tabIndex = 0;
    scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', '文字表格，可左右滚动');
    const element = document.createElement('table'); element.className = 'reader-reading-grid';
    element.setAttribute('aria-label', table.label + '的文字阅读视图');
    // 少列表格适应窄屏；列多时保留每列阅读宽度，由既有容器横向滚动。
    element.style.minWidth = `${Math.max(280, grid.column_count * 160)}px`;
    const occupied = new Set(), cells = new Map();
    grid.cells.forEach(cell => {
      cells.set(`${cell.row}:${cell.column}`, cell);
      for (let r = cell.row; r < cell.row + cell.row_span; r++)
        for (let c = cell.column; c < cell.column + cell.column_span; c++) occupied.add(`${r}:${c}`);
    });
    const body = document.createElement('tbody');
    for (let r = 1; r <= grid.row_count; r++) {
      const row = document.createElement('tr');
      for (let c = 1; c <= grid.column_count; c++) {
        const key = `${r}:${c}`, cell = cells.get(key);
        if (!cell && occupied.has(key)) continue;
        const value = document.createElement('td');
        if (cell) {
          value.rowSpan = cell.row_span; value.colSpan = cell.column_span;
          value.textContent = cell.lines.length ? cell.lines.map(line => line.text).join('\n') : '未读出';
          if (!cell.lines.length) {
            value.className = 'reader-grid-empty';
            if (typeof actions?.reread === 'function') {
              const button = document.createElement('button'); button.type = 'button';
              button.className = 'quiet-button'; button.textContent = '补读';
              button.setAttribute('aria-label', `补读第${cell.row}行第${cell.column}列`);
              const result = document.createElement('p'); result.className = 'reader-reread-result'; result.hidden = true;
              const original = document.createElement('button'); original.type = 'button'; original.className = 'quiet-button';
              original.textContent = '看原表'; original.hidden = true;
              original.addEventListener('click', () => { if (actions.current()) actions.original(); });
              button.addEventListener('click', async () => {
                if (!actions.current() || button.disabled) return;
                button.disabled = true; result.hidden = false; result.textContent = '正在本机补读……';
                try {
                  const reading = await actions.reread(cell);
                  if (!actions.current()) return;
                  result.textContent = reading.candidates.length
                    ? `补读候选：${reading.candidates[0].text}\n请对照原图，尚未核验。`
                    : '本次未读出文字，请看原图。';
                  button.textContent = '再次查看'; original.hidden = false;
                } catch (error) {
                  if (actions.current()) result.textContent = error.message === 'BUSY'
                    ? '另一项识别正在运行，请稍后重试。' : '此格暂不能补读，请看原图。';
                } finally { if (actions.current()) button.disabled = false; }
              });
              value.append(document.createElement('br'), button, result, original);
            }
          }
          const mark = selection?.table_id === table.id && selection.marks.find(mark => mark.cell === cell);
          if (mark) {
            value.className = `reader-grid-focus reader-grid-focus-${mark.role.toLowerCase()}`;
            value.setAttribute('data-cell-role',mark.role); value.setAttribute('aria-label',`${mark.label}：${value.textContent}`);
          }
        } else { value.textContent = '未定位'; value.className = 'reader-grid-empty'; }
        // 全部通过textContent显示；不把上传/识别文字解释为可执行HTML。
        row.append(value);
      }
      body.append(row);
    }
    element.append(body); scroll.append(element); container.replaceChildren(title, ...(captionLine ? [caption] : []), scroll);
    const remaining = grid.unassigned_lines.filter(line => line !== captionLine);
    if (remaining.length) {
      const details = document.createElement('details'), summary = document.createElement('summary');
      details.open = false; summary.textContent = `未归入格子的文字（${remaining.length} 行）`;
      details.append(summary);
      remaining.forEach(line => { const p = document.createElement('p'); p.textContent = line.text; details.append(p); });
      container.append(details);
    }
  }
  globalThis.ArchSpecTableReading = {valid, render, focus, highlightedTable};
})();
