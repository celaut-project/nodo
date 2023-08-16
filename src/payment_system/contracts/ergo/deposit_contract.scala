contract Deposit {

  // Declaración de variables de estado
  val parity_factor: Int
  val owner: Address
  val token_list: Collection[ByteString, Long]

  // Declaración de evento
  event NewSession(token: Coll[Byte], amount: Long)

  // Constructor
  constructor(pf: Int) {
    owner = INPUTS(0).R4[Address].get
    parity_factor = pf
    token_list = OUTPUTS(0).R6[Collection[ByteString, Long]].get
  }

  // Función de vista para obtener el factor de paridad
  def get_parity_factor(): Int = {
    parity_factor
  }

  // Función para agregar gas
  @payable
  def add_gas(token: Coll[Byte]) = {
    val newValue = SELF.R6[Collection[ByteString, Long]].getOrElse(OUTPUTS(0).id, {} : Collection[ByteString, Long])
    newValue[token] = newValue.getOrElse(token, 0L) + SELF.value
    val newOutputs = OUTPUTS.updated(0, OUTPUTS(0).updateR6(newValue))
    val newTx = TxUtil.createTx(1, INPUTS, newOutputs)
    sigmaProp(true, 21, SELF.id, newTx)
  }

  // Función de vista para obtener gas
  def get_gas(token: Coll[Byte]): Long = {
    token_list.getOrElse(token, 0L)
  }

  // Función de vista para obtener el propietario
  def get_owner(): Address = {
    owner
  }

  // Función para transferir la propiedad
  def transfer_property(new_owner: Address) = {
    val isOwner = owner == INPUTS(0).R4[Address].get
    val notNullOwner = new_owner != owner && new_owner != null
    val newOutputs = OUTPUTS.updated(0, OUTPUTS(0).updateR4(new_owner))
    val newTx = TxUtil.createTx(1, INPUTS, newOutputs)
    sigmaProp(isOwner && notNullOwner, 21, SELF.id, newTx)
  }

  // Función para reembolsar el saldo
  def refund_balance() = {
    val isOwner = owner == INPUTS(0).R4[Address].get
    val newOutputs = OUTPUTS.updated(0, OUTPUTS(0).updateValue(owner, SELF.value))
    val newTx = TxUtil.createTx(1, INPUTS, newOutputs)
    sigmaProp(isOwner, 21, SELF.id, newTx)
  }
}
