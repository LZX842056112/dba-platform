// 中国地图注册（懒加载）—— 只在真正出现 `chart.type="map"` 面板时才拉取 582KB GeoJSON。
//
// ★ 合规：GeoJSON 随仓库内置（`src/assets/geo/china-100000-full.json`），
//   运行时**不直连任何外部地图 CDN/瓦片服务**；领土要素含 34 省级（含台湾/港澳）
//   与九段线（南海诸岛），不得裁剪。来源与校验见同目录 README.md。

import { echarts } from './echarts';

/** 已注册的地图名（对应 GeoJSON 的 `properties.name` 为中国省级全称）。 */
export const CHINA_MAP_NAME = 'china';

let pending: Promise<void> | null = null;

/** 确保中国地图已注册（幂等；重复调用共用同一个 Promise）。 */
export function ensureChinaMap(): Promise<void> {
  if (!pending) {
    pending = import('../assets/geo/china-100000-full.json')
      .then((mod) => {
        echarts.registerMap(CHINA_MAP_NAME, mod.default as never);
      })
      .catch((err: unknown) => {
        // 注册失败不要静默：地图面板会退化为空图，需要能看到原因
        console.error('[dba] 中国地图 GeoJSON 注册失败', err);
        pending = null;
        throw err;
      });
  }
  return pending;
}
