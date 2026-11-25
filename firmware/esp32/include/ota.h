#pragma once
#include <Arduino.h>

#include "provisioning.h"

// Asks the backend whether a newer firmware exists. V1: logs the answer only (see ota.cpp).
void otaCheck(const NetConfig& cfg, const String& deviceId);
