-- Exercises the real inject decoder and net codec without a game process.
local root = assert(arg[1])
local function module(name, cm, k)
    assert(loadfile(root .. "/mod/mp_lockstep_1/res/scripts/mp/" .. name .. ".lua"))()(cm, k, function() end)
end
local function up(fn, wanted)
    for i = 1, 100 do
        local name, value = debug.getupvalue(fn, i)
        if not name then break end
        if name == wanted then return value end
    end
    error("missing upvalue " .. wanted)
end
local cm, k = {}, {INSTANCE = "A", INJECT_FILE = "test"}
module("net", cm, k)
local encode = up(cm.scheduleLocal, "encodeCmd")
local decode = up(up(cm.pollEvents, "onLine"), "decodeCmd")
local encoded = encode {op="LCREATE", at=1, origin="A", seq=2, name="007"}
local decoded = assert(decode(encoded))
assert(decoded.name == "007")
local captured
cm.peerSeen = true
cm.unescName = function(s) return s end
cm.scheduleLocal = function(op, args) assert(op == "LCREATE"); captured = args end
cm.readFrom = function() return "ARMED 1\nLCREATEX 0.498039216 0.25 1 180 0 name=Line\n", 1 end
module("inject", cm, k)
cm.pollInject()
assert(captured and captured.armed == 1)
assert(captured.color == "0.498039216,0.25,1")
-- The same Windows record has no Linux-only capture fields.
captured = nil
cm.readFrom = function() return "ARMED 1\nLCREATEX 1 0 0 180 0 name=Old\n", 2 end
cm.pollInject()
assert(captured and captured.name == "Old")
local roundTrip = decode(encode {op="LCREATE", at=1, origin="A", seq=3,
    name=captured.name, color=captured.color, wait=captured.wait, stops=captured.stops, armed=captured.armed})
assert(roundTrip.name == "Old" and roundTrip.armed == 1)

-- The unchanged 0.4.22 name/color reader always skips the origin, even for
-- ARMED 1. Linux must block these captures until it can replay them natively.
cm.lineKeyOf = {[12]="s:12"}; cm.vehKeyOf={}; cm.primedLines={}; cm.primedVeh={}
cm.lineKeyFor=function() return "s:12" end
cm.scheduleLocal=function(op,args) assert(op=="VNAME" or op=="VCOLOR"); captured=args end
for _,record in ipairs({"VNAME 12 Name", "VCOLOR 12 0.25 0.5 0.75"}) do
    captured=nil
    cm.readFrom=function() return "ARMED 1\n" .. record .. "\n",3 end
    cm.pollInject()
    assert(captured and captured.skipOrigin==1)
end
print("slice line wire: unchanged Windows create records and name/color origin-skip boundary passed")

-- Fractional/negative/infinite native waits survive the real Lua 5.2 wire path.
cm.stationGroupPos = function() return 1, 2 end
cm.stationPosInGroup = function() return nil end
cm.scheduleLocal = function(op, args) assert(op == "LCREATE"); captured = args end
cm.readFrom = function()
    return "ARMED 1\nLCREATEX 1 0 0 inf 1 98 100 200 3 -1.25 -inf 0 name=Waits\n", 4
end
captured = nil
cm.pollInject()
assert(captured and captured.wait == math.huge)
assert(captured.stops:find(",100,200,3,-1.25,-inf", 1, true))
local waits = decode(encode {op="LCREATE", at=1, origin="A", seq=4,
    wait=captured.wait, stops=captured.stops, armed=1})
assert(cm.waitNum(waits.wait) == math.huge and waits.stops == captured.stops)
print("slice line wire: fractional/negative/infinite waits passed")

-- Cargo regression: production capture/codec/build/snapshot code, with a table
-- model of the game API. This does not prove the engine's userdata properties.
package.path = root .. "/mod/mp_lockstep_1/res/scripts/?.lua;" .. package.path
local cargoCM, cargoK = {}, {INSTANCE="A", INJECT_FILE="test", LINE_EDIT_FREE=0}
module("net", cargoCM, cargoK)
module("lines", cargoCM, cargoK)
module("inject", cargoCM, cargoK)
local cfg = {load={1,0}, unload={0,1}, maxLoad={0.25,1}}
local suffix = "^1_0|0_1|0.25_1"
assert(cargoCM.lineCargoSuffix(cfg) == suffix)
assert(cargoCM.lineCargoSuffix(nil) == "")
assert(cargoCM.lineCargoSuffix({load={},unload={},maxLoad={}}) == "")
local base = "1.00,2.00,0,0,0,0,180"
assert(cargoCM.stopsSigEqual(base .. suffix, "1.50,2.00,0,0,0,0,180" .. suffix))
assert(not cargoCM.stopsSigEqual(base .. suffix, base .. "^0_0|0_1|0.25_1"))
assert(not cargoCM.stopsSigEqual(base .. suffix, base))
local records = {base, base}
cargoCM.lineCaptureCargo(" cfg=2:1_0|0_1|0.25_1", records)
assert(records[1] == base and records[2] == base .. suffix)
assert(not pcall(cargoCM.lineCaptureCargo, " cfg=3:1|2|3", records))
assert(not pcall(cargoCM.lineCaptureCargo, " cfg=1:bad", records))

local model = {fatInstances={{modelId=7, transf={[13]=3,[14]=4,[15]=5}}}}
local lineComponent
api = {
    type = {ComponentType={LINE=1,MODEL_INSTANCE_LIST=2,SIGNAL_LIST=3},
        Line={new=function() return {stops={}} end,
            Stop={new=function() return {stopConfig={load={},unload={},maxLoad={}},
                waypoints={},alternativeTerminals={}} end}},
        SignalId={new=function() return {} end}},
    engine = {entityExists=function() return true end,
        getComponent=function(id, ty)
            if ty == 1 then return lineComponent end
            if ty == 2 then return model end
            if ty == 3 then return {signals={{},{},{}}} end
        end},
    res={modelRep={getName=function() return "waypoint.mdl" end}}
}
game = {interface={getEntities=function() return {98} end, getName=function() return "Cargo" end}}
cargoCM.stationGroupPos=function() return 1,2 end
cargoCM.stationPosInGroup=function() return nil end
cargoCM.findStopNear=function() return 123 end
cargoCM.escName=function(s) return s end
cargoCM.unescName=function(s) return s end
cargoCM.peerSeen=true
cargoCM.lineHasVehicles=function() return true end
cargoCM.lineRekey(42,"s:42")
local wp = "~3.000:4.000:5.000:waypoint.mdl:2"
local build = up(cargoCM.lineApplyNow, "buildLineObject")
local cargoEncode = up(cargoCM.scheduleLocal, "encodeCmd")
local cargoDecode = up(up(cargoCM.pollEvents, "onLine"), "decodeCmd")
-- Per-cargo numeric flags, including zero entries at both ends. Exercise the
-- Windows 32-bit and Linux 64-bit word boundaries on the shared wire; this
-- fixture does not decode either platform's packed native storage.
for _,count in ipairs({2,30,33,65,1024}) do
    local load, unload, maximum = {}, {}, {}
    for i=1,count do
        load[i] = (i == 2 or i == 32 or i == 64) and 1 or 0
        unload[i] = 0
        maximum[i] = i == 2 and 0.25 or 1
    end
    local suffix = "^" .. table.concat(load,"_") .. "|" ..
        table.concat(unload,"_") .. "|" .. table.concat(maximum,"_")
    for _,op in ipairs({"LCREATE","LUPDATE"}) do
        local record = op == "LCREATE" and "LCREATEX 1 0 0 180" or "LUPDATE 42 180"
        record = record .. " 1 98 0 0 0 0 180 0 wp=1:123:2 cfg=1:" .. suffix:sub(2)
        if op == "LCREATE" then record = record .. " name=Cargo" end
        local result
        cargoCM.readFrom=function() return "ARMED 1\n" .. record .. "\n", 1 end
        cargoCM.scheduleLocal=function(got,args) assert(got == op); result=args end
        cargoCM.pollInject()
        assert(result and result.stops == base .. suffix .. wp)
        result.op=op; result.at=1; result.origin="A"; result.seq=10
        local wire=assert(cargoDecode(cargoEncode(result)))
        assert(wire.stops == result.stops)
        local obj,n=build(wire)
        assert(n == 1 and obj.stops[1].stationGroup == 98)
        assert(cargoCM.lineCargoSuffix(obj.stops[1].stopConfig) == suffix)
        assert(obj.stops[1].waypoints[1].entity == 123)
        assert(obj.stops[1].waypoints[1].index == 2)
        lineComponent=obj
        local snapshot=assert(cargoCM.lineSnapshot(42))
        assert(snapshot.stops == base .. suffix .. wp)
        lineComponent=nil
    end
end
local legacy=build{stops=base,wait=180}
assert(cargoCM.lineCargoSuffix(legacy.stops[1].stopConfig) == "")
print("slice line cargo: create/update capture, codec, replay, snapshot, equality and legacy records passed (mock API)")
-- Retain the Linux-only origin replay exception across the inject.lua merge.
cm.scheduleLocal=function(op,args) assert(op=="VNAME" or op=="VCOLOR"); captured=args end
for _,record in ipairs({"VNAME 12 Name replayOrigin=1", "VCOLOR 12 0.25 0.5 0.75 replayOrigin=1"}) do
    captured=nil
    cm.readFrom=function() return "ARMED 1\n" .. record .. "\n",5 end
    cm.pollInject()
    assert(captured and captured.skipOrigin==0)
end
print("slice line wire: native rename/color origin replay preserved")
