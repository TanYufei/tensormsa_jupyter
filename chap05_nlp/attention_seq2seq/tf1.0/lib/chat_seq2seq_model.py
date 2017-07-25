import tensorflow as tf
import tensorflow.contrib.seq2seq as seq2seq
from tensorflow.contrib.rnn import LSTMCell, LSTMStateTuple, GRUCell, MultiRNNCell
from tensorflow.contrib.rnn.python.ops.rnn_cell import _linear

from configs import model_config


class ChatSeq2SeqModel(object):
	def __init__(self, config, use_lstm=True, forward_only=False, bidirectional=True, attention=False):
		self.bidirectional = bidirectional
		self.attention = attention

		self.input_vocab_size = config.input_vocab_size
		self.target_vocab_size = config.target_vocab_size
		self.enc_hidden_size = config.enc_hidden_size
		self.enc_num_layers = config.enc_num_layers
		self.dec_hidden_size = config.dec_hidden_size
		self.dec_num_layers = config.dec_num_layers
		self.batch_size = config.batch_size

		self.learning_rate = tf.Variable(float(config.learning_rate), trainable=False)
		self.learning_rate_decay_op = self.learning_rate.assign(
			self.learning_rate * config.learning_rate_decay_factor)
		self.global_step = tf.Variable(0, trainable=False)
		self.max_gradient_norm = config.max_gradient_norm

		self.buckets = config.buckets

		# # If we use sampled softmax, we need an output projection.
		# output_projection = None
		# softmax_loss_function = None
		# # Sampled softmax only makes sense if we sample less than vocabulary size.
		# if num_samples > 0 and num_samples < self.target_vocab_size:
		# 	w = tf.get_variable("proj_w", [self.dec_hidden_size, self.target_vocab_size], initializer=tf.contrib.layers.xavier_initializer())
		# 	w_t = tf.transpose(w)
		# 	b = tf.get_variable("proj_b", [self.target_vocab_size], initializer=tf.contrib.layers.xavier_initializer())
		# 	output_projection = (w, b)
		#
		# 	def sampled_loss(inputs, labels):
		# 		labels = tf.reshape(labels, [-1, 1])
		# 		return tf.nn.sampled_softmax_loss(w_t, b, inputs, labels, num_samples, self.target_vocab_size)
		#
		# 	softmax_loss_function = sampled_loss

		# Create the internal multi-layer cell for our RNN.
		if use_lstm:
			single_cell1 = LSTMCell(self.enc_hidden_size)
			single_cell2 = LSTMCell(self.dec_hidden_size)
		else:
			single_cell1 = GRUCell(self.enc_hidden_size)
			single_cell2 = GRUCell(self.dec_hidden_size)
		enc_cell = MultiRNNCell([single_cell1 for _ in range(self.enc_num_layers)])
		dec_cell = MultiRNNCell([single_cell2 for _ in range(self.dec_num_layers)])

		self.encoder_cell = enc_cell
		self.decoder_cell = dec_cell

		self._make_graph(forward_only)
		self.saver = tf.train.Saver(tf.global_variables())

	def _make_graph(self, forward_only):
		self._init_data()
		self._init_embeddings()

		if self.bidirectional:
			self._init_bidirectional_encoder()
		else:
			self._init_simple_encoder()

		self._init_decoder(forward_only)

		if not forward_only:
			self._init_optimizer()

	def _init_data(self):
		""" Everything is time-major """
		self.encoder_inputs = tf.placeholder(shape=(None, None), dtype=tf.int32, name="encoder_inputs")
		self.encoder_inputs_length = tf.placeholder(shape=(None,), dtype=tf.int32, name="encoder_inputs_length")

		self.decoder_inputs = tf.placeholder(shape=(None, None), dtype=tf.int32, name="decoder_inputs")
		self.decoder_inputs_length = tf.placeholder(shape=(None,), dtype=tf.int32, name="decoder_inputs_length")

		# Our targets are decoder inputs shifted by one.
		self.decoder_targets = self.decoder_inputs[1:, :]
		self.target_weights = tf.ones([
			self.batch_size,
			tf.reduce_max(self.decoder_inputs_length)
		], dtype=tf.float32, name="loss_weights")

		temp_encoder_inputs = self.encoder_inputs[:self.buckets[-1][0], :]
		self.encoder_inputs2 = temp_encoder_inputs
		temp_decoder_inputs = self.decoder_inputs[:self.buckets[-1][1], :]
		self.decoder_inputs2 = temp_decoder_inputs

		self.target_weights = tf.placeholder(shape=(None, None), dtype=tf.float32, name="target_weights")

	def _init_embeddings(self):
		with tf.variable_scope("embedding") as scope:
			self.enc_embedding_matrix = tf.get_variable(
				name="enc_embedding_matrix",
				shape=[self.input_vocab_size, self.enc_hidden_size],
				initializer=tf.contrib.layers.xavier_initializer(),
				dtype=tf.float32)

			self.dec_embedding_matrix = tf.get_variable(
				name="dec_embedding_matrix",
				shape=[self.target_vocab_size, self.dec_hidden_size],
				initializer=tf.contrib.layers.xavier_initializer(),
				dtype=tf.float32)

			self.encoder_inputs_embedded = tf.nn.embedding_lookup(
				self.enc_embedding_matrix, self.encoder_inputs2)

			self.decoder_inputs_embedded = tf.nn.embedding_lookup(
				self.dec_embedding_matrix, self.decoder_inputs2)

	def _init_simple_encoder(self):
		with tf.variable_scope("encoder") as scope:
			(self.encoder_outputs, self.encoder_state) = tf.nn.dynamic_rnn(cell=self.encoder_cell,
																																		 inputs=self.encoder_inputs_embedded,
																																		 sequence_length=self.encoder_inputs_length,
																																		 time_major=True, dtype=tf.float32)

	def _init_bidirectional_encoder(self):
		with tf.variable_scope("biencoder") as scope:

			((encoder_fw_outputs, encoder_bw_outputs), (encoder_fw_state, encoder_bw_state)) = (
				tf.nn.bidirectional_dynamic_rnn(cell_fw=self.encoder_cell,
																				cell_bw=self.encoder_cell,
																				inputs=self.encoder_inputs_embedded,
																				sequence_length=self.encoder_inputs_length,
																				time_major=True,
																				dtype=tf.float32))

			self.encoder_outputs = tf.concat((encoder_fw_outputs, encoder_bw_outputs), 2)

			if isinstance(encoder_fw_state, LSTMStateTuple):
				encoder_state_c = tf.concat(
					(encoder_fw_state.c, encoder_bw_state.c), 1, name='bidirectional_concat_c')
				encoder_state_h = tf.concat(
					(encoder_fw_state.h, encoder_bw_state.h), 1, name='bidirectional_concat_h')
				self.encoder_state = LSTMStateTuple(c=encoder_state_c, h=encoder_state_h)

			elif isinstance(encoder_fw_state, tf.Tensor):
				self.encoder_state = tf.concat((encoder_fw_state, encoder_bw_state), 1, name='bidirectional_concat')

	def _init_decoder(self, forward_only):
		with tf.variable_scope("decoder") as scope:
			def output_fn(outputs):
				return tf.contrib.layers.linear(outputs, self.target_vocab_size, scope=scope)
			self.attention = True
			if not self.attention:
				if forward_only:
					decoder_fn = seq2seq.simple_decoder_fn_inference(
						output_fn=output_fn,
						encoder_state=self.encoder_state,
						embeddings=self.dec_embedding_matrix,
						start_of_sequence_id=model_config.GO_ID,
						end_of_sequence_id=model_config.EOS_ID,
						maximum_length=self.buckets[-1][1],
						num_decoder_symbols=self.target_vocab_size,
					)
					(self.decoder_outputs, self.decoder_state, self.decoder_context_state) = (
						seq2seq.dynamic_rnn_decoder(
							cell=self.decoder_cell,
							decoder_fn=decoder_fn,
							time_major=True,
							scope=scope,
						))
				else:
					decoder_fn = seq2seq.simple_decoder_fn_train(encoder_state=self.encoder_state)
					(self.decoder_outputs, self.decoder_state, self.decoder_context_state) = (
						seq2seq.dynamic_rnn_decoder(
							cell=self.decoder_cell,
							decoder_fn=decoder_fn,
							inputs=self.decoder_inputs_embedded,
							sequence_length=self.decoder_inputs_length,
							time_major=True,
							scope=scope,
						))

			else:
				# attention_states: size [batch_size, max_time, num_units]
				attention_states = tf.transpose(self.encoder_outputs, [1, 0, 2])

				(attention_keys, attention_values, attention_score_fn, attention_construct_fn) = (
					seq2seq.prepare_attention(
						attention_states=attention_states,
						attention_option="bahdanau",
						num_units=self.dec_hidden_size))

				if forward_only:
					decoder_fn = seq2seq.attention_decoder_fn_inference(
						output_fn=output_fn,
						encoder_state=self.encoder_state,
						attention_keys=attention_keys,
						attention_values=attention_values,
						attention_score_fn=attention_score_fn,
						attention_construct_fn=attention_construct_fn,
						embeddings=self.dec_embedding_matrix,
						start_of_sequence_id=model_config.GO_ID,
						end_of_sequence_id=model_config.EOS_ID,
						maximum_length=self.buckets[-1][1],
						num_decoder_symbols=self.target_vocab_size,
					)
					(self.decoder_outputs, self.decoder_state, self.decoder_context_state) = (
						seq2seq.dynamic_rnn_decoder(
							cell=self.decoder_cell,
							decoder_fn=decoder_fn,
							time_major=True,
							scope=scope,
						))
				else:
					decoder_fn = seq2seq.attention_decoder_fn_train(
						encoder_state=self.encoder_state,
						attention_keys=attention_keys,
						attention_values=attention_values,
						attention_score_fn=attention_score_fn,
						attention_construct_fn=attention_construct_fn,
						name='attention_decoder'
					)
					(self.decoder_outputs, self.decoder_state, self.decoder_context_state) = (
						seq2seq.dynamic_rnn_decoder(
							cell=self.decoder_cell,
							decoder_fn=decoder_fn,
							inputs=self.decoder_inputs_embedded,
							sequence_length=self.decoder_inputs_length,
							time_major=True,
							scope=scope,
						))

			if not forward_only:
				self.decoder_logits = output_fn(self.decoder_outputs)
			else:
				self.decoder_logits = self.decoder_outputs

			self.decoder_prediction = tf.argmax(self.decoder_logits, axis=-1, name='decoder_prediction')
			logits = tf.transpose(self.decoder_logits, [1, 0, 2])
			targets = tf.transpose(self.decoder_targets, [1, 0])

			if not forward_only:
				self.loss = seq2seq.sequence_loss(logits=logits, targets=targets,
																					weights=self.target_weights)

	def _init_optimizer(self):
		params = tf.trainable_variables()
		self.gradient_norms = []
		self.updates = []
		opt = tf.train.AdamOptimizer(self.learning_rate)
		gradients = tf.gradients(self.loss, params)
		clipped_gradients, norm = tf.clip_by_global_norm(gradients, self.max_gradient_norm)
		self.gradient_norms.append(norm)
		self.updates.append(opt.apply_gradients(zip(clipped_gradients, params), global_step=self.global_step))

	def step(self, session, encoder_inputs, encoder_inputs_length, decoder_inputs, decoder_inputs_length, target_weights, forward_only):
		# input_feed = {}
		# input_feed[self.encoder_inputs] = encoder_inputs
		# input_feed[self.encoder_inputs_length] = encoder_inputs_length
		# input_feed[self.decoder_inputs] = decoder_inputs
		# input_feed[self.decoder_inputs_length] = decoder_inputs_length
		input_feed = {
			self.encoder_inputs: encoder_inputs,
			self.encoder_inputs_length: encoder_inputs_length,
			self.decoder_inputs: decoder_inputs,
			self.decoder_inputs_length: decoder_inputs_length,
			self.target_weights: target_weights
		}

		if forward_only:
			output_feed = [self.decoder_logits, self.decoder_prediction, self.encoder_state, self.decoder_state]
			logits, prediction, encoder_embedding, decoder_embedding = session.run(output_feed, input_feed)
			return None, None, logits, prediction, encoder_embedding, decoder_embedding
		else:
			output_feed = [self.updates, self.gradient_norms, self.loss, self.encoder_state, self.decoder_state]
			updates, gradient, loss, encoder_embedding, decoder_embedding = session.run(output_feed, input_feed)
			return gradient, loss, None, None, encoder_embedding, decoder_embedding
