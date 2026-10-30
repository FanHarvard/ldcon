"""Physical contracts only: no database paths, metric names, labels or queries."""
from dataclasses import asdict,dataclass


@dataclass(frozen=True)
class Quantity:
    id:str
    unit:str
    meaning:str
    constraints:str


QUANTITIES={q.id:q for q in (
    Quantity('optical.frequency','Hz','Optical carrier frequency of the selected laser.','Positive; identify the optical channel and acquisition quality.'),
    Quantity('optical.wavelength','nm','Vacuum wavelength of the selected laser.','Positive; air wavelength requires an explicit refractive-index correction.'),
    Quantity('electrical.current','A','Measured laser diode injection current.','A controller command or voltage is not a measured current.'),
    Quantity('thermal.temperature','K','Measured temperature at the declared sensor location.','Not the temperature-controller command or TEC drive voltage.'),
    Quantity('control.current_voltage','V','Voltage applied to the current-controller input.','Not injection current; conversion requires separately verified calibration.'),
    Quantity('control.tec_voltage','V','Voltage applied to the TEC-controller input.','Not a measured temperature.'),
    Quantity('control.piezo_voltage','V','Measured voltage applied to the piezo actuator.','Not raw DAC counts; state which amplifier output is measured.'),
    Quantity('response.wavelength_per_current_voltage','nm/V','Local wavelength response to current-controller input modulation.','A local fitted response, not mode purity or distance to a mode boundary; fit validity required.'),
    Quantity('response.wavelength_per_current','nm/A','Local wavelength response to diode injection current modulation.','Distinct from response per input volt; fit validity and current calibration required.'),
)}

# Explicit dimension-preserving conversions, never implicit actuator calibration.
CONVERSIONS={('Hz','Hz'):(1.,0.),('MHz','Hz'):(1e6,0.),('GHz','Hz'):(1e9,0.),
             ('nm','nm'):(1.,0.),('m','nm'):(1e9,0.),('A','A'):(1.,0.),('mA','A'):(.001,0.),
             ('K','K'):(1.,0.),('degC','K'):(1.,273.15),('V','V'):(1.,0.),('mV','V'):(.001,0.),
             ('nm/V','nm/V'):(1.,0.),('nm/A','nm/A'):(1.,0.)}


def describe():return [asdict(q) for q in QUANTITIES.values()]


def conversion(quantity_id,source_unit):
    if quantity_id not in QUANTITIES:raise ValueError('Unknown physical quantity: '+str(quantity_id))
    try:return CONVERSIONS[(source_unit,QUANTITIES[quantity_id].unit)]
    except KeyError:raise ValueError('Incompatible source unit for '+quantity_id) from None
