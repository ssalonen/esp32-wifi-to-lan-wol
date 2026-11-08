#pragma once
#include <cstdint>

// Consume before arming so a subsequent reset cannot replay the same request.
template <typename Store>
uint32_t consumeBootTest(Store& store) {
  const uint32_t seconds = store.getUInt("delay", 0);
  if (!seconds || !store.remove("delay")) return 0;
  return seconds <= 3600 ? seconds : 0;
}
