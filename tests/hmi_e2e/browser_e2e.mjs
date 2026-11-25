// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

/**
 * The browser half of the Runtime v4 HMI end-to-end test (README.md): signs in at /hmi/ in Chromium
 * and drives the screens the browser runs, reading what the page sends and receives on /api/hmi.
 * e2e_hmi.py runs one scenario per call and checks the PLC side through the debugger.
 *
 *   node browser_e2e.mjs <scenario> '<json options>'
 *
 * Scenarios: operator-tap, viewer-refused, sign-out, module-access. Options: url, user, password, run (tag id),
 * level (tag id), status (page), shots (folder), playwright (path of the playwright package).
 * Prints one JSON line with the result; exits 1 with the reason on any failure. Every wait fails at
 * once when the page reports a failure (a notice other than the reconnect one, a page error).
 */
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const [scenario, raw] = process.argv.slice(2)
const opts = JSON.parse(raw ?? '{}')
const require = createRequire(import.meta.url)
const { chromium } = require(opts.playwright)

const FAILURE_NOTICES = [
  'HMI out of date, rebuild the project',
  'Device busy, try later',
  'The screen stopped on an error; restarting',
  'No HMI installed',
  'This browser cannot run the HMI',
]
const SWITCH = { x: 40, y: 34 }
const SIGN_OUT = { x: 400, y: 202 }

function fail(message) {
  throw new Error(message)
}

/**
 * Records the page's /api/hmi traffic: wraps window.io before the client script loads, so the events
 * the client sends and the values and refusals it receives are kept in window.__hmi, whatever the
 * transport (long polling or WebSocket).
 */
async function watchSocket(page) {
  await page.addInitScript(() => {
    const record = { values: [], sent: [], refused: [] }
    window.__hmi = record
    let real = null
    const wrapped = (...args) => {
      const socket = real(...args)
      const emit = socket.emit.bind(socket)
      socket.emit = (event, data, ...rest) => {
        if (event === 'in' && data) record.sent.push({ ...data, v: data.v ? Array.from(data.v) : undefined })
        return emit(event, data, ...rest)
      }
      const on = socket.on.bind(socket)
      socket.on = (event, handler) =>
        on(event, (...got) => {
          if (event === 'hmi_values') record.values.push(Array.from(new Uint8Array(got[0])))
          if (event === 'hmi_refused') record.refused.push(got[0])
          return handler(...got)
        })
      return socket
    }
    Object.defineProperty(window, 'io', {
      configurable: true,
      get: () => (real ? wrapped : undefined),
      set: (v) => {
        real = v
      },
    })
  })
  const seen = { values: [], sent: [], refused: [], read: 0 }
  seen.refresh = async () => {
    const got = await page.evaluate(() => window.__hmi)
    seen.sent = got.sent
    seen.refused = got.refused
    for (const bytes of got.values.slice(seen.read)) seen.values.push(decodeValues(Buffer.from(bytes)))
    seen.read = got.values.length
  }
  return seen
}

/** An hmi_values payload: page, write count and the records. */
function decodeValues(b) {
  const page = b.readUInt16LE(0)
  const writes = b.readUInt32LE(2)
  const count = b.readUInt16LE(6)
  const records = {}
  let off = 8
  for (let i = 0; i < count; i++) {
    const tag = b.readUInt16LE(off)
    const len = b.readUInt16LE(off + 2)
    records[tag] = [...b.subarray(off + 4, off + 4 + len)]
    off += 4 + len
  }
  return { page, writes, records }
}

const asFloat = (bytes) => Buffer.from(bytes).readFloatLE(0)

/** Polls pred until it holds; fails at once on a page error or a failure notice. */
async function waitUntil(page, errors, pred, what, timeoutMs = 15000) {
  const seen = page.__seen
  const end = Date.now() + timeoutMs
  for (;;) {
    if (errors.length > 0) fail(`${what}: the page failed: ${errors.join(' | ')}`)
    const notice = await page.evaluate(() => {
      const n = document.getElementById('notice')
      return n && !n.hidden ? n.textContent : ''
    })
    if (FAILURE_NOTICES.includes(notice)) fail(`${what}: the page shows "${notice}"`)
    if (seen) await seen.refresh()
    const value = await pred()
    if (value) return value
    if (Date.now() > end) fail(`${what}: not within ${timeoutMs} ms (notice "${notice}")`)
    await page.waitForTimeout(50)
  }
}

async function signIn(page, errors, user, password) {
  await page.goto(`${opts.url}/hmi/`)
  await page.fill('#username', user)
  await page.fill('#password', password)
  await page.click('#sign-in button')
  await waitUntil(
    page,
    errors,
    () => page.evaluate(() => document.getElementById('sign-in').hidden && document.getElementById('notice').hidden),
    `${user} signs in and the screens run`,
  )
}

/** A screenshot of the canvas once the frame after the last values was drawn (frame period 100 ms). */
async function canvasShot(page, name) {
  await page.waitForTimeout(300)
  mkdirSync(opts.shots, { recursive: true })
  const path = join(opts.shots, name)
  await page.locator('#surface').screenshot({ path })
  return path
}

async function tap(page, at) {
  await page.mouse.move(at.x, at.y)
  await page.mouse.down()
  await page.waitForTimeout(120)
  await page.mouse.up()
}

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 800, height: 480 } })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })
  const seen = await watchSocket(page)
  page.__seen = seen
  const out = { scenario }
  try {
    if (scenario === 'operator-tap') {
      await signIn(page, errors, opts.user, opts.password)
      const first = await waitUntil(
        page,
        errors,
        () => seen.values.find((v) => v.records[opts.run] && v.records[opts.level]),
        'the first values of the screen shown arrive',
      )
      out.runBefore = first.records[opts.run][0]
      if (out.runBefore !== 0) fail(`Run is ${out.runBefore} before the tap`)
      out.before = await canvasShot(page, 'operator-before-tap.png')
      await tap(page, SWITCH)
      const write = await waitUntil(
        page,
        errors,
        () => seen.sent.find((e) => e.t === 'w' && e.g === opts.run),
        'the tap on the switch sends a write',
      )
      out.write = write
      const levels = []
      await waitUntil(
        page,
        errors,
        () => {
          for (const v of seen.values) if (v.records[opts.level]) levels.push(asFloat(v.records[opts.level]))
          seen.values.length = 0
          return levels.length >= 3 && levels[levels.length - 1] > levels[0]
        },
        'Level rises in the values the browser receives',
      )
      out.levels = [levels[0], levels[levels.length - 1]]
      await page.waitForTimeout(500)
      out.after = await canvasShot(page, 'operator-after-tap.png')
    } else if (scenario === 'viewer-refused') {
      await signIn(page, errors, opts.user, opts.password)
      const show = await waitUntil(page, errors, () => seen.sent.find((e) => e.t === 's'), 'the screens ask for a screen')
      out.shown = show.p
      if (show.p !== opts.status) fail(`the viewer's screens show page ${show.p}, not Status (${opts.status})`)
      await waitUntil(
        page,
        errors,
        () => seen.values.find((v) => v.page === opts.status && v.records[opts.level]),
        'the Status screen gets its values',
      )
      out.screen = await canvasShot(page, 'viewer-status.png')
      await tap(page, SWITCH)
      await page.waitForTimeout(1000)
      await seen.refresh()
      out.writesFromTaps = seen.sent.filter((e) => e.t === 'w').length
      if (out.writesFromTaps !== 0) fail('a tap on the read-only screen sent a write')
      out.forged = await page.evaluate(
        ({ run, status, overview }) =>
          new Promise((resolve) => {
            const token = localStorage.getItem('openplc-hmi-token')
            const s = window.io('/api/hmi', { auth: { token }, forceNew: true })
            const got = { refused: [], statuses: [] }
            const done = () => {
              s.disconnect()
              resolve(got)
            }
            s.on('hmi_refused', (r) => {
              got.refused.push(r)
              if (got.refused.length === 2) done()
            })
            s.on('hmi_status', (st) => {
              got.statuses.push(st)
              if (st.state === 'live') {
                s.emit('in', { t: 's', p: overview, n: 1, h: st.build })
                s.emit('in', { t: 's', p: status, n: 2, h: st.build })
                s.emit('in', { t: 'w', g: run, v: new Uint8Array([1]), n: 3, h: st.build })
              }
            })
            s.on('connect', () => s.emit('in', { t: 'o', n: 0 }))
            setTimeout(done, 8000)
          }),
        { run: opts.run, status: opts.status, overview: opts.overview },
      )
      const refused = out.forged.refused
      if (!refused.some((r) => r.what === 'show' && r.id === opts.overview))
        fail(`the forged show of Overview was not refused: ${JSON.stringify(out.forged)}`)
      if (!refused.some((r) => r.what === 'write' && r.id === opts.run))
        fail(`the forged write of Run was not refused: ${JSON.stringify(out.forged)}`)
    } else if (scenario === 'sign-out') {
      await signIn(page, errors, opts.user, opts.password)
      await waitUntil(page, errors, () => seen.values.length > 0, 'the screens get values')
      const token = await page.evaluate(() => localStorage.getItem('openplc-hmi-token'))
      out.before = await canvasShot(page, 'before-sign-out.png')
      const logout = page.waitForResponse((r) => r.url().endsWith('/api/logout'), { timeout: 10000 })
      await tap(page, SIGN_OUT)
      await waitUntil(
        page,
        errors,
        () =>
          page.evaluate(
            () =>
              !document.getElementById('sign-in').hidden &&
              document.getElementById('sign-in-message').textContent === 'Signed out.',
          ),
        'the Sign out button returns to the form',
      )
      out.logout = (await logout).status()
      out.whoami = await page.evaluate(
        (t) => fetch('/api/whoami', { headers: { Authorization: `Bearer ${t}` } }).then((r) => r.status),
        token,
      )
      if (out.whoami !== 401) fail(`the signed-out token still answers ${out.whoami}`)
      out.form = join(opts.shots, 'signed-out.png')
      await page.screenshot({ path: out.form })
    } else if (scenario === 'module-access') {
      const wasmResponses = []
      page.on('response', (r) => {
        if (r.url().endsWith('/hmi/hmi.wasm')) wasmResponses.push(r.status())
      })
      await page.goto(`${opts.url}/hmi/`)
      await page.waitForSelector('#sign-in:not([hidden])')
      out.beforeSignIn = await page.evaluate(() =>
        fetch('hmi.wasm').then(async (r) => ({ status: r.status, bytes: (await r.arrayBuffer()).byteLength })),
      )
      if (out.beforeSignIn.status !== 401) fail(`hmi.wasm without a token answered ${out.beforeSignIn.status}`)
      out.loadedBeforeSignIn = wasmResponses.filter((s) => s === 200).length
      if (out.loadedBeforeSignIn !== 0) fail('the sign-in page loaded hmi.wasm before sign-in')
      const nav = await page.goto(`${opts.url}/hmi/hmi.wasm`)
      out.navigation = { url: page.url(), status: nav ? nav.status() : 0, type: nav ? nav.headers()['content-type'] : '' }
      if (!page.url().endsWith('/hmi/') || !String(out.navigation.type).startsWith('text/html'))
        fail(`a page load of hmi.wasm without a token was not sent to the sign-in page: ${JSON.stringify(out.navigation)}`)
      /* The refused probe logs its own 401 in the console: that and nothing else is expected so far. */
      const unexpected = errors.splice(0).filter((e) => !/status of 401/.test(e))
      if (unexpected.length > 0) fail(`the page failed before sign-in: ${unexpected.join(' | ')}`)
      await signIn(page, errors, opts.user, opts.password)
      await waitUntil(page, errors, () => seen.values.length > 0, 'the screens get values')
      out.loadedSignedIn = wasmResponses.filter((s) => s === 200).length
      if (out.loadedSignedIn !== 1) fail(`hmi.wasm loaded ${out.loadedSignedIn} times once signed in`)
      const token = await page.evaluate(() => localStorage.getItem('openplc-hmi-token'))
      out.signedIn = await page.evaluate(
        (t) => fetch('hmi.wasm', { headers: { Authorization: `Bearer ${t}` } }).then((r) => r.status),
        token,
      )
      out.shot = await canvasShot(page, 'module-signed-in.png')
      const logout = page.waitForResponse((r) => r.url().endsWith('/api/logout'), { timeout: 10000 })
      await tap(page, SIGN_OUT)
      await waitUntil(
        page,
        errors,
        () => page.evaluate(() => !document.getElementById('sign-in').hidden),
        'the Sign out button returns to the form',
      )
      out.logout = (await logout).status()
      out.afterSignOut = await page.evaluate(
        (t) => fetch('hmi.wasm', { headers: { Authorization: `Bearer ${t}` } }).then((r) => r.status),
        token,
      )
      if (out.signedIn !== 200 || out.afterSignOut !== 401)
        fail(`hmi.wasm signed in ${out.signedIn}, after sign-out ${out.afterSignOut}`)
    } else {
      fail(`unknown scenario ${scenario}`)
    }
    console.log(JSON.stringify(out))
  } catch (e) {
    console.log(JSON.stringify({ ...out, error: e.message }))
    process.exitCode = 1
  } finally {
    await browser.close()
  }
}

await main()
