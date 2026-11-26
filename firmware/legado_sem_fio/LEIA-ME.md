# Firmware antigo (bancada + joysticks sem fio) — NÃO usar na montagem atual

Estes firmwares eram para a primeira ideia de hardware: uma ESP32-S3 em cada joystick, com
bateria, falando sem fio (ESP-NOW) com uma ESP32 na bancada. A montagem real é outra: **uma
ESP32 só, na caixa, com todos os sensores ligados por fio**. O firmware certo é
`firmware/caixa/caixa.ino`.

Ficam aqui só como referência. O programa Trilho ainda reconhece esse protocolo (os testes
automáticos usam), mas ele não é mais mantido.
