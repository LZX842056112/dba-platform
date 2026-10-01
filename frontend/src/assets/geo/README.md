# 中国地图 GeoJSON（合规留痕）

## 文件

| 文件 | 说明 |
|---|---|
| `china-100000-full.json` | 中国全图（省级边界 + 九段线），供 ECharts `registerMap` 使用 |

## 来源与校验

| 项 | 值 |
|---|---|
| 来源 URL | `https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json` |
| 抓取日期 | 2026-10-01 |
| 字节数 | 582522 |
| SHA256 | `99adfeded5223848bbe37a0a12f8023e11ee12161c7800521c27db42fdeac275` |
| 用途 | 大屏 `chart.type="map"` 面板的分级设色底图 |

## 领土完整性校验（★ 必须通过，不得裁剪）

抓取后已用脚本核验以下条件，全部通过：

| 检查项 | 结果 |
|---|---|
| 省级行政区数量 | **34** 个 ✅ |
| 含台湾省（`adcode=710000`） | ✅ |
| 含香港特别行政区（`810000`） | ✅ |
| 含澳门特别行政区（`820000`） | ✅ |
| 含九段线 / 南海诸岛（`adcode=100000_JD`） | ✅（MultiPolygon，290 点，经度 108.2~122.8 / 纬度 3.4~24.6，即南海海域） |

## 使用约定（合规红线）

1. **本文件随仓库内置，运行时绝不直连任何外部地图 CDN/瓦片服务**。
2. **禁止裁剪、过滤、简化任何要素**——尤其不得移除 `100000_JD`（九段线）或任何省级要素。
3. **禁止手工编辑边界坐标**；如需更新，重新抓取并重跑完整性校验，更新本文件与 SHA256。
4. **展示不得裁切**：`geo` 的 `center`/`zoom`/`roam` 不得把南海诸岛移出可视区。
5. **仅使用国产合规地图源**：腾讯 / 高德 / 百度 / 天地图。禁止 Google Maps、Apple Maps、Bing（海外）、OpenStreetMap 直连瓦片、Mapbox、Leaflet+OSM 等境外源。
6. 不得标注军事禁区、涉密单位或未公开的敏感坐标。
7. 涉及个人位置数据时须遵守《个人信息保护法》，不得收集/存储/公开他人位置。

## 更新流程

```bash
# 1) 重新抓取
python -c "import urllib.request,pathlib;url='https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json';pathlib.Path('frontend/src/assets/geo/china-100000-full.json').write_bytes(urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})).read())"
# 2) 重跑完整性校验（34 省级 + 台湾 + 港澳 + 九段线），并更新本文件的 SHA256 与日期
```
