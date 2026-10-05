const BASE_URL = import.meta.env.BASE_URL || '/'

/**
 * Real YOLO-World outputs generated from 4K WebP demo assets.
 * Keep the source and license metadata next to the sample so attribution
 * cannot drift when the gallery is reused by other views.
 */
export const VISION_SAMPLES = [
  {
    id: 'plastic-bottle',
    title: '滩涂混合垃圾中的塑料瓶',
    category: '塑料容器',
    summary: '在木料、泡沫和渔网混杂的强干扰场景中定位蓝色瓶体。',
    image: 'demo/beach-mixed-debris-4k.webp',
    result: 'demo/beach-mixed-debris-detections.json',
    status: 'verified',
    statusLabel: '主目标命中',
    reviewText: '单框命中，瓶体边界与蓝色目标一致。',
    source: {
      title: 'Beach trash (30870156434)',
      author: 'Justin Dolske',
      license: 'CC BY-SA 2.0',
      licenseUrl: 'https://creativecommons.org/licenses/by-sa/2.0/',
      pageUrl: 'https://commons.wikimedia.org/wiki/File:Beach_trash_(30870156434).jpg',
      originalSize: '4896×3264',
    },
  },
  {
    id: 'discarded-shoes',
    title: '海岸回收筐中的废旧鞋类',
    category: '生活塑料',
    summary: '验证密集堆叠、相互遮挡和多材质鞋底的开放词汇召回。',
    image: 'demo/midway-shoes-4k.webp',
    result: 'demo/midway-shoes-detections.json',
    status: 'review',
    statusLabel: '密集召回',
    reviewText: '模型输出 6 个候选框，大框存在聚合，适合验证召回而非直接判定数量。',
    source: {
      title: 'Marine Debris on Midway Atoll (33068857593)',
      author: 'USFWS Pacific',
      license: 'Public domain',
      licenseUrl: 'https://creativecommons.org/publicdomain/mark/1.0/',
      pageUrl: 'https://commons.wikimedia.org/wiki/File:Marine_Debris_on_Midway_Atoll_(33068857593).jpg',
      originalSize: '6000×4000',
    },
  },
  {
    id: 'fishing-gear',
    title: '海滩废弃渔网与绳索',
    category: '渔具',
    summary: '从蓝色、绿色混合绳网中定位可回收渔具区域。',
    image: 'demo/fishing-net-4k.webp',
    result: 'demo/fishing-net-detections.json',
    status: 'verified',
    statusLabel: '主体命中',
    reviewText: '单框落在绳网主体；远距离和低对比场景仍建议人工复核。',
    source: {
      title: 'Fis01486 (27554975513)',
      author: 'NOAA Photo Library',
      license: 'CC BY 2.0',
      licenseUrl: 'https://creativecommons.org/licenses/by/2.0/',
      pageUrl: 'https://commons.wikimedia.org/wiki/File:Fis01486_(27554975513).jpg',
      originalSize: '4608×3456',
    },
  },
  {
    id: 'polystyrene-cup',
    title: '风化的破损聚苯乙烯杯',
    category: '泡沫',
    summary: '在小目标与自然杂物混杂的岸线环境中识别泡沫制品。',
    image: 'demo/styrofoam-cup-4k.webp',
    result: 'demo/styrofoam-cup-detections.json',
    status: 'review',
    statusLabel: '低置信度命中',
    reviewText: '单框覆盖杯体主体，置信度适中；上线前应补充同域样本校准。',
    source: {
      title: 'Physical weathering styrofoam cup Lake MIchigan',
      author: 'Visviva',
      license: 'CC0',
      licenseUrl: 'https://creativecommons.org/publicdomain/zero/1.0/',
      pageUrl:
        'https://commons.wikimedia.org/wiki/File:Physical_weathering_styrofoam_cup_Lake_MIchigan.jpg',
      originalSize: '4016×3008',
    },
  },
]

export function visionAssetUrl(relativePath) {
  const normalizedBase = BASE_URL.endsWith('/') ? BASE_URL : `${BASE_URL}/`
  return `${normalizedBase}${String(relativePath || '').replace(/^\/+/, '')}`
}

export async function loadVisionSample(sample) {
  const response = await fetch(visionAssetUrl(sample.result), { cache: 'no-store' })
  if (!response.ok) {
    throw new Error(`样例数据加载失败（HTTP ${response.status}）`)
  }

  const payload = await response.json()
  const item = payload?.items?.[0]
  if (!item || !Array.isArray(item.detections)) {
    throw new Error('样例数据格式不完整')
  }

  return {
    ...item,
    engine: 'YOLO-World v2',
    model: payload.model,
    promptClasses: payload.prompt_classes || [],
    confidenceThreshold: payload.confidence_threshold,
    iouThreshold: payload.iou_threshold,
    inferenceSize: payload.image_size,
    sampleId: sample.id,
    sample,
  }
}
