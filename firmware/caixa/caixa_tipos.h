// Tipos usados pelo caixa.ino.
// Ficam num arquivo separado porque a Arduino IDE coloca declarações das funções no
// começo do .ino, antes do resto do código: se estes tipos estivessem no .ino, a
// compilação daria "'Imu' was not declared in this scope".
#pragma once
#include <Arduino.h>
#include <Wire.h>

struct Imu {
  char id;              // 'A' janela, 'B' espiral, 'C' carro
  TwoWire* bus;
  uint8_t end;
  bool ok;
  uint8_t falhas;
  int64_t proxima;
  uint8_t quem;         // WHO_AM_I: 0x68 MPU6050, 0x70 MPU6500, 0x71 MPU9250, 0x73 MPU9255 (0 = não lido)
};

struct Ir {
  volatile bool emTransicao;
  volatile int64_t tPrimeira, tUltima;
  uint8_t reportado;
};
