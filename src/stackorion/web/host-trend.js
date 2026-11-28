// Provider-independent series presentation. The acquisition layer supplies values and units.
const hostMetricGroups = [
  { name:'CPU 使用率', icon:'activity', series:[['cpu_percent','CPU']], unit:'%', max:100 },
  { name:'内存使用率', icon:'server', series:[['memory_percent','内存']], unit:'%', max:100 },
  { name:'根分区使用率', icon:'layers', series:[['disk_percent','磁盘']], unit:'%', max:100 },
  { name:'网络收发', icon:'route', series:[['network_rx_kbit','接收'],['network_tx_kbit','发送']], unit:' kbit' },
];

export function metricGroupsFor(samples) {
  const rate = samples.some(({metrics}) => 'network_rx_kbps' in metrics || 'network_tx_kbps' in metrics);
  if (!rate) return hostMetricGroups;
  return [...hostMetricGroups.slice(0, -1), {
    ...hostMetricGroups.at(-1), name:'网络速率', unit:' kbit/s',
    series:[['network_rx_kbps','接收'], ['network_tx_kbps','发送']],
  }];
}

const tickClock = new Intl.DateTimeFormat('zh-CN', {
  timeZone:'Asia/Shanghai', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23',
});
const detailClock = new Intl.DateTimeFormat('zh-CN', {
  timeZone:'Asia/Shanghai', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit',
  second:'2-digit', hourCycle:'h23',
});
const compactClock = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});

export function formattedSample(point, unit) {
  return point ? point.value.toLocaleString('zh-CN', { maximumFractionDigits:2 }) + unit : '未取得';
}

function align(samples, group, intervalMinutes) {
  const times = [];
  const values = group.series.map(() => []);
  let previous;
  for (const sample of [...samples].reverse()) {
    const epoch = Date.parse(sample.observed_at) / 1000;
    if (!Number.isFinite(epoch) || (previous && epoch <= previous.epoch)) continue;
    const interval = intervalMinutes || sample.interval_minutes || previous?.sample.interval_minutes;
    if (interval && previous && (intervalMinutes || sample.trigger === 'scheduled' || previous.sample.trigger === 'scheduled')
        && epoch - previous.epoch > interval * 90) {
      times.push(previous.epoch + (epoch - previous.epoch) / 2);
      for (const series of values) series.push(null);
    }
    times.push(epoch);
    group.series.forEach(([key], index) => values[index].push(sample.metrics[key]?.value ?? null));
    previous = {epoch, sample};
  }
  return [times, ...values];
}

function percentRange(data, zoomY) {
  if (!zoomY) return [0, 100];
  const values = data[1].filter((value) => value !== null);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const padding = Math.max(0.1, (high - low) * 0.2);
  return [Math.max(0, Math.floor((low - padding) * 10) / 10),
    Math.min(100, Math.ceil((high + padding) * 10) / 10)];
}

export function mountHostTrend(container, group, samples, resetButton, zoomY = false, {compact = false, intervalMinutes} = {}) {
  const data = align(samples, group, intervalMinutes);
  if (!data.slice(1).some((series) => series.some((value) => value !== null))) return null;
  const UPlot = globalThis.uPlot;
  if (!UPlot) throw new Error('uPlot unavailable');
  const theme = getComputedStyle(container);
  const color = theme.getPropertyValue('--host-chart-color').trim();
  const area = theme.getPropertyValue('--host-chart-fill').trim();
  const grid = theme.getPropertyValue('--line').trim();
  const ink = theme.getPropertyValue('--muted').trim();
  const tooltip = document.createElement('div');
  tooltip.className = 'host-chart-tooltip';
  tooltip.hidden = true;
  tooltip.setAttribute('aria-hidden', 'true');
  const [first] = data[0];
  const last = data[0].at(-1);
  const bounds = first === last ? {min:first - 60, max:last + 60} : {min:first, max:last};
  const size = () => ({width:Math.max(container.clientWidth, compact ? 1 : 180),
    height:compact ? 90 : container.clientWidth < 580 ? 230 : 300});
  const options = {
    ...size(),
    cursor:{drag:{x:!compact, y:false, setScale:!compact}, points:{size:7, width:2}},
    legend:{show:false},
    scales:{
      x:{time:true, min:bounds.min, max:bounds.max},
      y:{range: group.max ? percentRange(data, zoomY) : (_chart, _min, max) => [0, Math.max(1, max * 1.1)]},
    },
    axes:[
      {stroke:ink, size:compact ? 28 : 36, space:compact ? 44 : 92, font:compact ? '11px system-ui' : '12px system-ui', grid:{show:false}, ticks:{stroke:grid},
        values:(_chart, ticks) => ticks.map((value) => (compact ? compactClock : tickClock).format(new Date(value * 1000)))},
      {stroke:ink, size:compact ? 36 : 56, space:compact ? 26 : 42, font:compact ? '11px system-ui' : '12px system-ui', grid:{stroke:grid, width:1, dash:[4,4]},
        ticks:{show:false},
        values:(_chart, ticks) => ticks.map((value) => value.toLocaleString('zh-CN', {maximumFractionDigits:2}) + (group.max ? '%' : ''))},
    ],
    series:[
      {},
      ...group.series.map(([, label], index) => ({
        label, stroke:color, width:2, dash:index ? [5,4] : undefined, spanGaps:false,
        paths:compact ? UPlot.paths.spline() : undefined,
        points:{show:compact || data[index + 1].filter((value) => value !== null).length <= 12, size:compact ? 4 : 6},
        fill:index ? undefined : (chart) => {
          const gradient = chart.ctx.createLinearGradient(0, chart.bbox.top, 0, chart.bbox.top + chart.bbox.height);
          gradient.addColorStop(0, area);
          gradient.addColorStop(1, 'transparent');
          return gradient;
        },
      })),
    ],
    hooks:{
      setCursor:[(plot) => {
        const index = plot.cursor.idx;
        tooltip.hidden = index == null || plot.cursor.left < 0;
        if (tooltip.hidden) return;
        const time = detailClock.format(new Date(data[0][index] * 1000));
        const value = group.series.map(([, label], series) => {
          const sample = data[series + 1][index];
          return label + ' ' + formattedSample(sample === null ? null : { value:sample }, group.unit);
        }).join(' · ');
        tooltip.replaceChildren(document.createTextNode(time + ' UTC+8'), document.createElement('br'), document.createTextNode(value));
      }],
      setScale:[(_plot, key) => {
        if (resetButton && key === 'x' && first !== last) resetButton.hidden = false;
      }],
    },
  };
  const plot = new UPlot(options, data, container);
  plot.root.setAttribute('role', 'img');
  plot.root.setAttribute('aria-label', group.name + '历史趋势图，详细采集时间和数值见采集明细。');
  container.append(tooltip);
  if (resetButton) resetButton.hidden = first === last;
  const resize = new ResizeObserver(() => {
    const {width, height} = size();
    if (plot.width !== width || plot.height !== height) plot.setSize({width, height});
  });
  resize.observe(container);
  return {
    reset() { plot.setScale('x', bounds); },
    destroy() {
      resize.disconnect();
      plot.destroy();
      tooltip.remove();
      if (resetButton) resetButton.hidden = true;
    },
  };
}
