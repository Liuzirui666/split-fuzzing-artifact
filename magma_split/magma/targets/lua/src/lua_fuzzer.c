/*
 * libFuzzer harness for Lua (Magma).
 *
 * Magma's lua target builds the *standalone* Lua interpreter for the AFL /
 * honggfuzz / MOpt fuzzers, which feed each test case as a Lua script via
 * stdin. libFuzzer is in-process and cannot drive a standalone main(); it
 * needs an LLVMFuzzerTestOneInput entry point linked against the fuzzing
 * engine. This file provides exactly that: it loads the input bytes as a Lua
 * chunk and (if it parses) executes it, exercising the same lexer / parser /
 * VM that the AFL-family fuzzers reach via the interpreter. The Magma bug
 * detectors compiled into liblua.a fire identically under both paths.
 *
 * Mirrors the canonical OSS-Fuzz lua_fuzzer.
 */
#include <stdint.h>
#include <stddef.h>

#include "lua.h"
#include "lauxlib.h"
#include "lualib.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    if (size == 0) {
        return 0;
    }

    lua_State *L = luaL_newstate();
    if (L == NULL) {
        return 0;
    }

    luaL_openlibs(L);

    if (luaL_loadbuffer(L, (const char *)data, size, "fuzz") == LUA_OK) {
        lua_pcall(L, 0, 0, 0);
    }

    lua_close(L);
    return 0;
}
