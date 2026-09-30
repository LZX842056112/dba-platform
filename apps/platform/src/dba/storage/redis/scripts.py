"""Lua 原子脚本集合（★ P0-4 / P1-9 的核心）。

为什么必须用 Lua（照抄 v1 的「先读后写」会怎样错）
------------------------------------------------
v1 的预算是「HGET 判断 → HINCRBY 累加」两步走。并发下 N 个请求可能都读到「未超限」
然后一起 HINCRBY，于是**超卖**（reserved 之和超过 limit）。Redis 单线程执行 Lua，
把「判断 + 累加」放进一个脚本即天然原子，杜绝超卖。

★ 幂等语义（P0-4）用返回值编码，调用方据此决定是否改 MySQL：
  * ``1`` = 本次真正生效（granted / settled / released）
  * ``2`` = 幂等命中（已存在 / 已结算 / 已释放）→ **不得重复改数**
  * ``0`` = 未命中（拒绝 / 预留不存在）
"""

from __future__ import annotations

__all__ = [
    "SCRIPTS",
    "RESERVE",
    "SETTLE",
    "RELEASE",
    "TOKEN_BUCKET",
]

#: 预算预留：原子「判断 + 累加」，并写入预留状态（幂等依据）
RESERVE = """
-- KEYS[1]=usage hash  KEYS[2]=reservation hash
-- ARGV[1]=amount ARGV[2]=hard_limit ARGV[3]=reservation_id ARGV[4]=ttl_s
local uk = KEYS[1]
local rk = KEYS[2]
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[4])

-- 幂等：同一 reservation_id 重复预留直接返回 2（不重复占用）
if redis.call('EXISTS', rk) == 1 then
  return {2, tonumber(redis.call('HGET', rk, 'estimated') or '0')}
end

local consumed = tonumber(redis.call('HGET', uk, 'consumed') or '0')
local reserved = tonumber(redis.call('HGET', uk, 'reserved') or '0')
local used = consumed + reserved

if used + amount > limit then
  return {0, used}
end

redis.call('HINCRBY', uk, 'reserved', amount)
redis.call('HSET', rk, 'state', 'RESERVED', 'estimated', amount,
           'budget_id', ARGV[5], 'period_start', ARGV[6])
redis.call('EXPIRE', rk, ttl)
return {1, used + amount}
"""

#: 结算：RESERVED → SETTLED；已结算返回 2（幂等不改数）
SETTLE = """
-- KEYS[1]=usage hash  KEYS[2]=reservation hash
-- ARGV[1]=actual_micro_usd
local uk = KEYS[1]
local rk = KEYS[2]
local actual = tonumber(ARGV[1])

if redis.call('EXISTS', rk) == 0 then
  return {0, 0}
end
local state = redis.call('HGET', rk, 'state')
if state ~= 'RESERVED' then
  return {2, tonumber(redis.call('HGET', rk, 'estimated') or '0')}
end
local est = tonumber(redis.call('HGET', rk, 'estimated') or '0')
redis.call('HSET', rk, 'state', 'SETTLED', 'actual', actual)
redis.call('HINCRBY', uk, 'reserved', -est)
redis.call('HINCRBY', uk, 'consumed', actual)
return {1, est}
"""

#: 释放：RESERVED → RELEASED；已释放/已结算返回 2（幂等不改数）
RELEASE = """
-- KEYS[1]=usage hash  KEYS[2]=reservation hash
local uk = KEYS[1]
local rk = KEYS[2]

if redis.call('EXISTS', rk) == 0 then
  return {0, 0}
end
local state = redis.call('HGET', rk, 'state')
if state ~= 'RESERVED' then
  return {2, tonumber(redis.call('HGET', rk, 'estimated') or '0')}
end
local est = tonumber(redis.call('HGET', rk, 'estimated') or '0')
redis.call('HSET', rk, 'state', 'RELEASED')
redis.call('HINCRBY', uk, 'reserved', -est)
return {1, est}
"""

#: 令牌桶限流（原子补充 + 扣减）
TOKEN_BUCKET = """
-- KEYS[1]=bucket key
-- ARGV[1]=rate(per s) ARGV[2]=burst ARGV[3]=now_ms ARGV[4]=cost
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = burst end
if ts == nil then ts = now end

local refill = math.max(0, now - ts) / 1000.0 * rate
tokens = math.min(burst, tokens + refill)

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 60000)
return {allowed, math.floor(tokens)}
"""

#: 脚本名 → 源码（供 SCRIPT LOAD / register_script）
SCRIPTS: dict[str, str] = {
    "budget_reserve": RESERVE,
    "budget_settle": SETTLE,
    "budget_release": RELEASE,
    "token_bucket": TOKEN_BUCKET,
}
