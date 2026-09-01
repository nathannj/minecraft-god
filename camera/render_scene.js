/*
 * Render a private, player-independent scene snapshot from the live /godscan
 * result.  This is intentionally a small voxel renderer rather than a second
 * Minecraft client: it has no account, no camera entity, and no effect on the
 * player's view.  The resulting PNGs are visual evidence for the god, while
 * the block/entity data in the scan remains authoritative.
 */

const fs = require('fs')
const path = require('path')
const { createCanvas } = require('canvas')

function parsePosition (value) {
  const parts = String(value || '').trim().split(/\s+/).map(Number)
  if (parts.length !== 3 || parts.some(Number.isNaN)) return null
  return { x: parts[0], y: parts[1], z: parts[2] }
}

function blockPosition (block) {
  return parsePosition(block.position) || parsePosition(block.relative)
}

function blockColor (type) {
  const name = String(type || '').replace(/^minecraft:/, '').toLowerCase()
  const named = {
    air: '#b7d9e8',
    stone: '#858585', cobblestone: '#777777', deepslate: '#444b52',
    dirt: '#8b5a35', coarse_dirt: '#76502f', grass_block: '#5f9b45',
    sand: '#d9c27c', red_sand: '#b86d43', gravel: '#9a9790',
    oak_log: '#76502d', spruce_log: '#4c3422', birch_log: '#cfbb82',
    oak_planks: '#b88a52', spruce_planks: '#765335', birch_planks: '#cbb47b',
    glass: '#a8d9df', water: '#3d86c6', lava: '#e85b24',
    blue_wool: '#3158a5', light_blue_wool: '#6aa7d7',
    red_wool: '#a83232', white_wool: '#e5e5df', black_wool: '#252525',
    yellow_wool: '#d7b632', gold_block: '#e2bd27', iron_block: '#d4d8d9',
    glowstone: '#f2c86e', sea_lantern: '#9bd6d0', lantern: '#d18d38',
    torch: '#f4ad40', redstone_torch: '#c23e35',
    oak_sign: '#a97943', spruce_sign: '#5b3e28', birch_sign: '#d1bc7f',
    stone_bricks: '#777777', bricks: '#a94e3d', obsidian: '#27213e'
  }
  if (named[name]) return named[name]
  let hash = 0
  for (const char of name) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0
  const hue = hash % 360
  return `hsl(${hue}, 35%, 52%)`
}

function shade (hex, amount) {
  if (!hex.startsWith('#') || hex.length !== 7) return hex
  const values = [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16))
  return '#' + values.map(value => Math.max(0, Math.min(255, Math.round(value * amount))).toString(16).padStart(2, '0')).join('')
}

function drawPolygon (ctx, points, fill, stroke = '#20252a') {
  ctx.beginPath()
  ctx.moveTo(points[0][0], points[0][1])
  for (const point of points.slice(1)) ctx.lineTo(point[0], point[1])
  ctx.closePath()
  ctx.fillStyle = fill
  ctx.fill()
  ctx.strokeStyle = stroke
  ctx.lineWidth = 0.6
  ctx.stroke()
}

function fitIso (blocks, width, height) {
  const positions = blocks.map(blockPosition).filter(Boolean)
  if (!positions.length) return { scale: 24, ox: width / 2, oy: height / 2, minX: 0, minY: 0, minZ: 0, maxX: 1, maxY: 1, maxZ: 1 }
  const minX = Math.min(...positions.map(pos => pos.x))
  const maxX = Math.max(...positions.map(pos => pos.x)) + 1
  const minY = Math.min(...positions.map(pos => pos.y))
  const maxY = Math.max(...positions.map(pos => pos.y)) + 1
  const minZ = Math.min(...positions.map(pos => pos.z))
  const maxZ = Math.max(...positions.map(pos => pos.z)) + 1
  const rawWidth = (maxX - minX + maxZ - minZ) * 0.5
  const rawHeight = (maxX - minX + maxZ - minZ) * 0.25 + (maxY - minY) * 0.5
  const scale = Math.max(7, Math.min(34, Math.min((width - 80) / rawWidth, (height - 130) / rawHeight)))
  const project = (x, y, z) => [(x - z) * scale * 0.5, (x + z) * scale * 0.25 - (y - minY) * scale * 0.5]
  const corners = [project(minX, minY, minZ), project(maxX, minY, maxZ), project(minX, maxY, minZ), project(maxX, maxY, maxZ)]
  const left = Math.min(...corners.map(point => point[0]))
  const right = Math.max(...corners.map(point => point[0]))
  const top = Math.min(...corners.map(point => point[1]))
  const bottom = Math.max(...corners.map(point => point[1]))
  // Center the fitted scene in the usable canvas area.  The first version
  // pinned the top edge to the header, which made small builds look lost in
  // a large field of background.
  return { scale, ox: width / 2 - (left + right) / 2, oy: (height + 54) / 2 - (top + bottom) / 2, minX, minY, minZ, maxX, maxY, maxZ, project }
}

function drawIso (scan, width, height) {
  const canvas = createCanvas(width, height)
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = '#dce8ed'
  ctx.fillRect(0, 0, width, height)
  const blocks = Array.isArray(scan.blocks) ? scan.blocks.filter(block => blockPosition(block)) : []
  const fit = fitIso(blocks, width, height)
  const project = (x, y, z) => {
    const point = fit.project ? fit.project(x, y, z) : [x, y]
    return [point[0] + fit.ox, point[1] + fit.oy]
  }
  const sorted = blocks.slice().sort((a, b) => {
    const pa = blockPosition(a); const pb = blockPosition(b)
    return (pa.x + pa.z + pa.y * 0.1) - (pb.x + pb.z + pb.y * 0.1)
  })
  for (const block of sorted) {
    const pos = blockPosition(block)
    const p000 = project(pos.x, pos.y, pos.z)
    const p100 = project(pos.x + 1, pos.y, pos.z)
    const p010 = project(pos.x, pos.y + 1, pos.z)
    const p001 = project(pos.x, pos.y, pos.z + 1)
    const p101 = project(pos.x + 1, pos.y, pos.z + 1)
    const p011 = project(pos.x, pos.y + 1, pos.z + 1)
    const p110 = project(pos.x + 1, pos.y + 1, pos.z)
    const p111 = project(pos.x + 1, pos.y + 1, pos.z + 1)
    const base = blockColor(block.type)
    drawPolygon(ctx, [p010, p110, p111, p011], shade(base, 1.12))
    drawPolygon(ctx, [p010, p011, p001, p000], shade(base, 0.78))
    drawPolygon(ctx, [p010, p000, p100, p110], shade(base, 0.94))
    if (Array.isArray(block.front_text) && block.front_text.some(Boolean)) {
      const center = project(pos.x + 0.5, pos.y + 1.05, pos.z + 0.03)
      ctx.fillStyle = 'rgba(40, 25, 15, .9)'
      ctx.fillRect(center[0] - 22, center[1] - 13, 44, 23)
      ctx.fillStyle = '#fff5c7'
      ctx.font = '8px sans-serif'
      block.front_text.filter(Boolean).slice(0, 3).forEach((line, index) => ctx.fillText(String(line).slice(0, 11), center[0] - 20, center[1] - 5 + index * 8))
    }
  }
  drawEntities(ctx, scan.entities, project)
  drawHeader(ctx, scan, 'PRIVATE ISOMETRIC SCENE')
  return canvas.toBuffer('image/png')
}

function drawTopDown (scan, width, height) {
  const canvas = createCanvas(width, height)
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = '#eef4f6'; ctx.fillRect(0, 0, width, height)
  const blocks = Array.isArray(scan.blocks) ? scan.blocks.map(block => ({ block, pos: blockPosition(block) })).filter(item => item.pos) : []
  const minX = blocks.length ? Math.min(...blocks.map(item => item.pos.x)) : 0
  const maxX = blocks.length ? Math.max(...blocks.map(item => item.pos.x)) : 1
  const minZ = blocks.length ? Math.min(...blocks.map(item => item.pos.z)) : 0
  const maxZ = blocks.length ? Math.max(...blocks.map(item => item.pos.z)) : 1
  const cell = Math.max(8, Math.min(30, Math.min((width - 90) / Math.max(1, maxX - minX + 1), (height - 140) / Math.max(1, maxZ - minZ + 1))))
  const ox = (width - (maxX - minX + 1) * cell) / 2
  const oy = 75
  const highest = new Map()
  for (const item of blocks) {
    const key = `${item.pos.x},${item.pos.z}`
    if (!highest.has(key) || highest.get(key).pos.y < item.pos.y) highest.set(key, item)
  }
  for (const item of highest.values()) {
    const x = ox + (item.pos.x - minX) * cell
    const y = oy + (item.pos.z - minZ) * cell
    ctx.fillStyle = blockColor(item.block.type); ctx.fillRect(x, y, cell - 1, cell - 1)
    ctx.strokeStyle = 'rgba(0,0,0,.25)'; ctx.strokeRect(x, y, cell - 1, cell - 1)
  }
  for (const entity of Array.isArray(scan.entities) ? scan.entities : []) {
    const pos = parsePosition(entity.position); if (!pos) continue
    const x = ox + (pos.x - minX + 0.5) * cell; const y = oy + (pos.z - minZ + 0.5) * cell
    ctx.fillStyle = '#7b2cbf'; ctx.beginPath(); ctx.arc(x, y, Math.max(4, cell * 0.22), 0, Math.PI * 2); ctx.fill()
    if (entity.custom_name) { ctx.fillStyle = '#28113f'; ctx.font = '11px sans-serif'; ctx.fillText(String(entity.custom_name).slice(0, 22), x + 5, y - 5) }
  }
  drawHeader(ctx, scan, 'PRIVATE TOP-DOWN SCENE')
  return canvas.toBuffer('image/png')
}

function drawEntities (ctx, entities, project) {
  for (const entity of Array.isArray(entities) ? entities : []) {
    const pos = parsePosition(entity.position); if (!pos) continue
    const point = project(pos.x, pos.y, pos.z)
    ctx.fillStyle = '#7b2cbf'; ctx.beginPath(); ctx.arc(point[0], point[1] - 8, 5, 0, Math.PI * 2); ctx.fill()
    ctx.fillStyle = '#28113f'; ctx.font = '11px sans-serif'
    const label = entity.custom_name || entity.type || 'entity'
    ctx.fillText(String(label).slice(0, 24), point[0] + 7, point[1] - 5)
  }
}

function drawHeader (ctx, scan, title) {
  ctx.fillStyle = '#17252d'; ctx.fillRect(0, 0, ctx.canvas.width, 54)
  ctx.fillStyle = '#f5fbff'; ctx.font = 'bold 18px sans-serif'; ctx.fillText(title, 18, 23)
  ctx.font = '12px sans-serif'
  const meta = `${scan.dimension || 'unknown'}  |  player ${scan.player || '?'}  |  blocks ${Array.isArray(scan.blocks) ? scan.blocks.length : 0}  |  ${scan.block_position || ''}`
  ctx.fillText(meta.slice(0, 150), 18, 42)
}

async function main () {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'))
  const scan = input.scan || input
  const outputDir = path.resolve(input.output_dir || path.join(__dirname, '..', 'state', 'captures'))
  const width = Math.max(320, Math.min(1600, Number(input.width) || 900))
  const height = Math.max(240, Math.min(1200, Number(input.height) || 650))
  fs.mkdirSync(outputDir, { recursive: true })
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)
  const base = `scene-${stamp}-${process.pid}`
  const images = []
  const isoPath = path.join(outputDir, `${base}-isometric.png`)
  fs.writeFileSync(isoPath, drawIso(scan, width, height)); images.push({ view: 'isometric', path: isoPath })
  const topPath = path.join(outputDir, `${base}-topdown.png`)
  fs.writeFileSync(topPath, drawTopDown(scan, width, height)); images.push({ view: 'topdown', path: topPath })
  process.stdout.write(JSON.stringify({ ok: true, renderer: 'live-godscan-voxel-camera', images, block_count: Array.isArray(scan.blocks) ? scan.blocks.length : 0, entity_count: Array.isArray(scan.entities) ? scan.entities.length : 0 }))
}

main().catch(error => {
  process.stdout.write(JSON.stringify({ ok: false, renderer: 'live-godscan-voxel-camera', error: error.message }))
  process.exitCode = 1
})
